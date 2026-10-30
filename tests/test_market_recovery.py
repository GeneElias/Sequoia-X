from unittest.mock import Mock

import pandas as pd
import pytest

from sequoia_x.reporting.market import History, MarketData, MarketDataError
from tests.test_reporting import history


def test_tencent_keeps_raw_prices_and_reported_turnover(monkeypatch):
    raw=[['2026-10-29','99','100','102','98','1000'],['2026-10-30','100','102','103','99','2000']]
    adjusted=[['2026-10-29','49.5','50','51','49','1000'],['2026-10-30','50','51','51.5','49.5','2000']]
    quote=['0']*40;quote[3]='102';quote[6]='2000';quote[30]='20261030160000';quote[37]='2020'
    get=Mock(side_effect=[Mock(json=lambda:{'data':{'sz300750':{'qfqday':adjusted}}}),Mock(json=lambda:{'data':{'sz300750':{'day':raw,'qt':{'sz300750':quote}}}})])
    monkeypatch.setattr('requests.get',get)
    h=MarketData._tencent('300750','A','2026-10-30')
    assert h.frame.loc['2026-10-30','close']==51
    assert h.frame.loc['2026-10-30','raw_close']==102
    assert h.frame.loc['2026-10-30','volume']==200000
    assert h.frame.loc['2026-10-30','amount']==20200000
    assert h.frame.loc['2026-10-29','amount']!=h.frame.loc['2026-10-29','amount']
    assert all(call.kwargs['timeout']==12 for call in get.call_args_list)


def test_tencent_unknown_adjustment_not_silently_promoted(monkeypatch):
    get=Mock(return_value=Mock(json=lambda:{'data':{'sz300750':{'day':[['2026-10-30',1,1,1,1,1]]}}}))
    monkeypatch.setattr('requests.get',get)
    with pytest.raises(ValueError,match='显式复权'):MarketData._tencent('300750','A','2026-10-30')


def test_failed_adapters_expose_safe_validation_reason_not_vendor_secret(tmp_path,monkeypatch):
    provider=MarketData(tmp_path)
    primary=Mock(side_effect=RuntimeError('https://SECRET'));primary.__name__='_yahoo'
    secondary=Mock(side_effect=ValueError('无效低价'));secondary.__name__='_sina'
    monkeypatch.setattr(provider,'_yahoo',primary);monkeypatch.setattr(provider,'_sina',secondary)
    with pytest.raises(MarketDataError) as exc:provider.history('MSFT','US','2026-10-30')
    assert 'SECRET' not in str(exc.value) and '无效低价' in str(exc.value)


def test_a_raw_quotes_can_use_independent_event_coverage(tmp_path,monkeypatch):
    provider=MarketData(tmp_path)
    frame=pd.DataFrame({'raw_close':[100.,102.],'raw_open':[99.,100.], 'raw_high':[101.,103.], 'raw_low':[98.,99.]},index=['2026-10-29','2026-10-30'])
    h=History('300750','A',frame,'Tencent','Tencent','consistent','CNY')
    ev=History('300750','A',pd.DataFrame({'raw_close':[100.,102.],'stock_splits':[0.,0.]},index=pd.to_datetime(frame.index)),'yfinance','Yahoo Finance','consistent','CNY')
    monkeypatch.setattr(provider,'history',lambda *a:h)
    monkeypatch.setattr(provider,'_yahoo',lambda *a:ev)
    assert provider.trading_history('300750','A','2026-10-30').frame.stock_splits.tolist()==[0,0]
    ev.frame['raw_close']=[100.,120.]
    with pytest.raises(ValueError,match='差异超过1%'):provider.trading_history('300750','A','2026-10-30')
