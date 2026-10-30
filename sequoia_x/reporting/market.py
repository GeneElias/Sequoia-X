"""Market data adapters and closed-session validation.

Sina routing/field mapping follows global-stock-data's us_stock_kline_sina:
https://github.com/simonlin1212/global-stock-data.
We do not execute downloaded skill code. Unknown adjustment is quarantined.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re

import pandas as pd
import requests


class MarketDataError(ValueError):
    """Safe adapter/stage diagnostics, never raw provider exceptions."""


VALIDATION_REASONS = {
    '缺少 OHLCV 列','重复交易日','没有完整日线','日线有缺失或非有限值','价格必须为正',
    '无效高价或成交量','无效低价','缺少未复权收盘价','缺少有效的未复权收盘价',
    '不足121根日线或数据并非最新完整交易日','最近121个交易日不连续（停牌或日线缺失），不参与RPS',
}


@dataclass
class History:
    symbol: str
    market: str
    frame: pd.DataFrame
    source: str
    upstream: str
    adjustment: str
    currency: str
    warnings: list[str] = field(default_factory=list)


def latest_session(market: str, now: datetime | None = None) -> str:
    """Calendar closes include holidays, US DST and half-days; allow publication lag."""
    import exchange_calendars as xcals

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must include timezone")
    cutoff = pd.Timestamp(now - timedelta(minutes=30))
    day = cutoff.date()
    cal = xcals.get_calendar(
        "XNYS" if market == "US" else "XSHG",
        start=str(day - timedelta(days=45)), end=str(day + timedelta(days=2)),
    )
    closed = [s for s in cal.sessions if cal.session_close(s) <= cutoff]
    if not closed:
        raise ValueError("交易日历中没有完整收盘日")
    return str(closed[-1].date())


def normalize(frame: pd.DataFrame, as_of: str) -> pd.DataFrame:
    frame = frame.copy()
    frame.columns = [str(c).lower().replace(" ", "_") for c in frame.columns]
    required = ["open", "high", "low", "close", "volume"]
    if not set(required) <= set(frame.columns):
        raise ValueError("缺少 OHLCV 列")
    frame.index = pd.Index(pd.to_datetime(frame.index).strftime("%Y-%m-%d"), name="date")
    if frame.index.duplicated().any():
        raise ValueError("重复交易日")
    frame = frame.sort_index().loc[lambda f: f.index <= as_of]
    if frame.empty:
        raise ValueError("没有完整日线")
    for col in required:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    values = frame[required]
    if values.isna().any().any() or values.isin([float('inf'), -float('inf')]).any().any():
        raise ValueError("日线有缺失或非有限值")
    if (frame[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("价格必须为正")
    if (frame.volume < 0).any() or (frame.high < frame[["open", "close", "low"]].max(axis=1)).any():
        raise ValueError("无效高价或成交量")
    if (frame.low > frame[["open", "close", "high"]].min(axis=1)).any():
        raise ValueError("无效低价")
    if "raw_close" not in frame:
        raise ValueError("缺少未复权收盘价")
    frame["raw_close"] = pd.to_numeric(frame.raw_close, errors="coerce")
    if frame.raw_close.isna().any() or frame.raw_close.isin([float('inf'), -float('inf')]).any() or (frame.raw_close <= 0).any():
        raise ValueError("缺少有效的未复权收盘价")
    return frame


def validate_window(frame: pd.DataFrame, market: str, as_of: str) -> None:
    import exchange_calendars as xcals

    if len(frame) < 121 or frame.index[-1] != as_of:
        raise ValueError("不足121根日线或数据并非最新完整交易日")
    cal = xcals.get_calendar("XNYS" if market == "US" else "XSHG",
                             start=frame.index[-121], end=as_of)
    if list(frame.index[-121:]) != [str(s.date()) for s in cal.sessions]:
        raise ValueError("最近121个交易日不连续（停牌或日线缺失），不参与RPS")


class MarketData:
    def __init__(self, cache: Path, refresh: bool = False):
        self.cache = cache
        self.refresh = refresh
        cache.mkdir(parents=True, exist_ok=True)

    def history(self, symbol: str, market: str, as_of: str) -> History:
        if not re.fullmatch(r"[A-Z0-9.^=-]{1,20}", symbol) or market not in {"US", "A"}:
            raise ValueError("无效市场或代码")
        path = self.cache / f"{market}-{symbol}-{as_of}.json"
        if path.exists() and not self.refresh:
            obj = json.loads(path.read_text())
            frame = pd.DataFrame(obj.pop("rows")).set_index("date")
            frame = normalize(frame, as_of)
            validate_window(frame, market, as_of)
            return History(symbol, market, frame, **obj)
        attempts = []
        adapters = [self._yahoo, self._sina] if market == "US" else [self._akshare, self._tencent, self._yahoo]
        for adapter in adapters:
            try:
                h = adapter(symbol, market, as_of)
                h.frame = normalize(h.frame, as_of)
                validate_window(h.frame, market, as_of)
                # Failed fetches and unverified fallback data are not retained as good cache.
                h.warnings = attempts + h.warnings
                obj = dict(source=h.source, upstream=h.upstream, adjustment=h.adjustment,
                           currency=h.currency, warnings=h.warnings,
                           rows=json.loads(h.frame.reset_index().to_json(orient="records")))
                if h.adjustment == "consistent":
                    path.write_text(json.dumps(obj, ensure_ascii=False, allow_nan=False))
                return h
            except Exception as exc:
                # URLs may contain tokens: store only adapter name and exception type.
                reason = str(exc) if isinstance(exc, ValueError) and str(exc) in VALIDATION_REASONS else type(exc).__name__
                attempts.append(f"{adapter.__name__[1:]}: {reason}")
        raise MarketDataError("取数失败：" + "; ".join(attempts))

    @staticmethod
    def _yahoo(symbol: str, market: str, as_of: str) -> History:
        import yfinance as yf

        ticker = symbol if market == "US" else symbol + (".SS" if symbol.startswith("6") else ".SZ")
        df = yf.Ticker(ticker).history(period="2y", auto_adjust=False, timeout=12)
        if df.empty or "Adj Close" not in df or "Close" not in df:
            raise ValueError("Yahoo 无完整复权数据")
        factor = df["Adj Close"] / df["Close"]
        # All OHLC share one total-return adjustment; raw close is retained for valuation.
        out = pd.DataFrame({c.lower(): df[c] * factor for c in ["Open", "High", "Low", "Close"]})
        out["volume"] = df["Volume"]
        out["raw_close"] = df["Close"]
        out["raw_open"] = df["Open"]
        out["raw_high"] = df["High"]
        out["raw_low"] = df["Low"]
        out["dividends"] = df.get("Dividends", 0.0)
        if "Stock Splits" in df:
            out["stock_splits"] = df["Stock Splits"]
        return History(symbol, market, out, "yfinance", "Yahoo Finance", "consistent",
                       "USD" if market == "US" else "CNY",
                       ["成交量为供应商报告值；复权口径见完整报告"])

    def trading_history(self, symbol: str, market: str, as_of: str) -> History:
        """Paper fills need raw OHLC and split events, not unknown Sina adjustments."""
        required = {"raw_open", "raw_high", "raw_low", "stock_splits"}
        h = self.history(symbol, market, as_of)
        if h.upstream == "Yahoo Finance" and required <= set(h.frame.columns):
            return h
        events = self._yahoo(symbol, market, as_of)
        if market == 'A' and {'raw_open','raw_high','raw_low'} <= set(h.frame.columns):
            # Raw OHLC can come from Eastmoney/Tencent; Yahoo supplies dated split
            # events even when an OHLC field in its own latest bar is malformed.
            event_frame = events.frame.copy()
            event_frame.index = pd.to_datetime(event_frame.index).strftime('%Y-%m-%d')
            event_frame = event_frame.loc[event_frame.index <= as_of]
            if event_frame.empty or event_frame.index[-1] != as_of or 'stock_splits' not in event_frame:
                raise ValueError('拆股事件覆盖不足')
            aligned = event_frame.reindex(h.frame.index)
            if aligned.stock_splits.isna().any() or aligned.raw_close.isna().any():
                raise ValueError('拆股事件存在缺口')
            if ((aligned.raw_close / h.frame.raw_close - 1).abs() > .01).any():
                raise ValueError('两源原始收盘价差异超过1%，暂停模拟成交')
            h.frame['stock_splits'] = aligned.stock_splits
            return h
        events.frame = normalize(events.frame, as_of)
        validate_window(events.frame, market, as_of)
        if not required <= set(events.frame.columns):
            raise ValueError("模拟成交缺少原始价格或拆股事件")
        return events

    @staticmethod
    def _akshare(symbol: str, market: str, as_of: str) -> History:
        import akshare as ak

        start = (pd.Timestamp(as_of) - pd.Timedelta(days=650)).strftime("%Y%m%d")
        end = as_of.replace("-", "")
        # Fetch raw and adjusted explicitly; adjustment must not contaminate turnover.
        adjusted = ak.stock_zh_a_hist(symbol=symbol, start_date=start, end_date=end, adjust="qfq", timeout=8)
        raw = ak.stock_zh_a_hist(symbol=symbol, start_date=start, end_date=end, adjust="", timeout=8)
        mapping = {"日期": "date", "开盘": "open", "最高": "high", "最低": "low",
                   "收盘": "close", "成交量": "volume", "成交额": "amount"}
        frame = adjusted.rename(columns=mapping).set_index("date")
        original = raw.rename(columns=mapping).set_index("date")
        for column in ('open','high','low','close'):
            frame['raw_'+column] = original[column]
        frame["amount"] = original["amount"]
        frame["volume"] = original["volume"] * 100  # Eastmoney: lots -> shares.
        return History(symbol, market, frame[["open", "high", "low", "close", "volume", "raw_open", "raw_high", "raw_low", "raw_close", "amount"]],
                       "AKShare", "Eastmoney", "consistent", "CNY")

    @staticmethod
    def _tencent(symbol: str, market: str, as_of: str) -> History:
        code = ('sh' if symbol.startswith('6') else 'sz') + symbol
        url = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
        frames, payloads = {}, {}
        for adjust in ('qfq', ''):
            response = requests.get(url, params={'param':f'{code},day,,{as_of},640,{adjust}'}, timeout=12)
            response.raise_for_status()
            payload = response.json()['data'][code]
            key = 'qfqday' if adjust else 'day'
            rows = payload.get(key)
            # Never claim an unadjusted fallback is consistently adjusted.
            if not rows:raise ValueError('腾讯缺少显式复权或原始日线')
            frame = pd.DataFrame([r[:6] for r in rows], columns=['date','open','close','high','low','volume']).set_index('date')
            frame = frame.apply(pd.to_numeric, errors='raise')
            frames[adjust], payloads[adjust] = frame, payload
        frame, raw = frames['qfq'], frames['']
        if not frame.index.equals(raw.index):raise ValueError('腾讯复权与原始交易日不一致')
        for col in ('open','high','low','close'):frame['raw_'+col] = raw[col]
        frame['volume'] = raw['volume'] * 100  # Tencent K-line volume is lots.
        quote = payloads[''].get('qt', {}).get(code, [])
        if len(quote) > 37 and str(quote[30])[:8] == as_of.replace('-',''):
            # Quote turnover is reported in RMB10,000; never approximate it by close*volume.
            if abs(float(quote[3]) / float(raw.loc[as_of,'close']) - 1) <= .001 and abs(float(quote[6]) - float(raw.loc[as_of,'volume'])) <= 1:
                frame['amount'] = float('nan')
                frame.loc[as_of,'amount'] = float(quote[37]) * 10000
        return History(symbol, market, frame, 'Tencent K-line', 'Tencent', 'consistent', 'CNY',
                       ['腾讯前复权与原始日线分别读取；历史成交额缺失时不生成海龟成交额确认'])

    @staticmethod
    def _sina(symbol: str, market: str, as_of: str) -> History:
        response = requests.get(
            "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var/US_MinKService.getDailyK",
            params={"symbol": symbol, "num": 500},
            headers={"Referer": "https://finance.sina.com.cn/"}, timeout=15,
        )
        response.raise_for_status()
        match = re.search(r"\((\[.*\])\)", response.text, re.S)
        if not match:
            raise ValueError("无效新浪 JSONP")
        rows = json.loads(match.group(1))
        frame = pd.DataFrame(rows).rename(columns={"d": "date", "o": "open", "h": "high",
                                                        "l": "low", "c": "close", "v": "volume"}).set_index("date")
        frame["raw_close"] = frame["close"]
        return History(symbol, market, frame, "global-stock-data 路由适配", "Sina", "unknown", "USD",
                       ["备源复权口径未经核实：仅展示数据，不生成买入信号"])
