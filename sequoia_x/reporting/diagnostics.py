"""Read-only live probes, saved only inside the encrypted state branch."""
import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
import sys

import requests

from .github_state import GitHubState
from .market import MarketData, latest_session, normalize, validate_window


def main():
    root = Path('reports')
    state = GitHubState(Path.cwd(), root, os.environ['SEQUOIA_STATE_KEY'].encode())
    state.restore()
    result = {'generated_at': datetime.now(timezone.utc).isoformat(), 'quotes': [], 'financials': {}, 'runs': []}
    provider = MarketData(root / '_state/cache')
    # Limit each request, including third-party SDK calls which omit timeouts.
    original = requests.sessions.Session.request
    def bounded(self, method, url, **kw):
        kw['timeout'] = min(kw.get('timeout') or 15, 15)
        return original(self, method, url, **kw)
    requests.sessions.Session.request = bounded
    with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        if '--probe' in sys.argv:
            result['network_probes'] = []
            for label,agent in [('project_url','Sequoia-X research https://github.com/GeneElias/Sequoia-X'),('short_contact','Sequoia-X research geneelias@users.noreply.github.com'),('project_name','Sequoia-X research GeneElias/Sequoia-X')]:
                try:
                    res=requests.get('https://data.sec.gov/api/xbrl/companyfacts/CIK0000789019.json',headers={'User-Agent':agent},timeout=15)
                    result['network_probes'].append({'ua':label,'status':res.status_code,'content_type':res.headers.get('Content-Type')})
                except Exception as exc:result['network_probes'].append({'ua':label,'error':type(exc).__name__})
            result['tencent_probe'] = []
            for adj in ('qfq',''):
                try:
                    res=requests.get('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get',params={'param':f'sz300750,day,,{latest_session("A")},640,{adj}'},timeout=12)
                    payload=res.json()['data']['sz300750']
                    result['tencent_probe'].append({'adjust':adj,'keys':list(payload),'qfq_last':payload.get('qfqday',[])[-3:],'raw_last':payload.get('day',[])[-3:], 'lengths':{k:len(payload.get(k,[])) for k in ['day','qfqday']}, 'first':{k:payload.get(k,[])[:1] for k in ['day','qfqday']}, 'qt':payload.get('qt',{}).get('sz300750',[])[:40]})
                except Exception as exc:result['tencent_probe'].append({'error':type(exc).__name__})
            (root/'_state/diagnostics.json').write_text(json.dumps(result,ensure_ascii=False,default=str))
            state.save()
            return
        if '--full' in sys.argv:
            from .fundamentals import Fundamentals
            from .report import DEFAULT_UNIVERSE, build_report
            result['repair_preview'] = []
            for market in ('A','US'):
                valuations = Fundamentals(root/'_state/fundamentals.json').refresh(market,DEFAULT_UNIVERSE[market],latest_session(market),force=True)
                report = build_report({market:DEFAULT_UNIVERSE[market]},provider,valuations)
                result['repair_preview'].append(report)
            (root/'_state/diagnostics.json').write_text(json.dumps(result,ensure_ascii=False,default=str))
            state.save()
            return
        for symbol in ['600519', '000858', '300750']:
            for method in ['_akshare', '_yahoo']:
                row = {'symbol': symbol, 'adapter': method}; start=time.monotonic()
                try:
                    h = getattr(provider, method)(symbol, 'A', latest_session('A'))
                    row.update(length=len(h.frame), last=str(h.frame.index[-1]))
                    try:
                        h.frame = normalize(h.frame, latest_session('A'))
                    except ValueError as exc:
                        row['normalize_reason'] = str(exc)
                        f=h.frame.copy(); f.columns=[str(c).lower().replace(' ','_') for c in f.columns]
                        row['bad_high'] = f.index[f.high < f[['open','close','low']].max(axis=1)].astype(str).tolist()[-15:]
                        row['bad_low'] = f.index[f.low > f[['open','close','high']].min(axis=1)].astype(str).tolist()[-15:]
                        raise
                    try:
                        validate_window(h.frame, 'A', latest_session('A')); row['status']='ok'
                    except ValueError as exc:
                        row['status']='window'; row['reason']=str(exc)
                        import exchange_calendars as xc
                        cal=xc.get_calendar('XSHG',start=h.frame.index[-121],end=latest_session('A'))
                        row['missing'] = sorted(set(str(s.date()) for s in cal.sessions)-set(h.frame.index))
                except Exception as exc:
                    row['status']=type(exc).__name__
                row['seconds']=round(time.monotonic()-start,2);result['quotes'].append(row)
        import yfinance as yf
        for symbol in ['MSFT','600519.SS','000858.SZ']:
            try:
                t=yf.Ticker(symbol)
                result['financials'][symbol] = {'info':{k:t.info.get(k) for k in ['currency','financialCurrency','sharesOutstanding','sector','industry','totalDebt','totalCash']},'statements':{name:json.loads(getattr(t,name).iloc[:,:3].to_json(date_format='iso')) for name in ['cash_flow','income_stmt','balance_sheet']}}
            except Exception as exc:result['financials'][symbol]={'error':type(exc).__name__}
        import akshare as ak
        for symbol in ['sh600519','sz000858']:
            result['financials'][symbol]={}
            for name in ['资产负债表','利润表','现金流量表']:
                try:
                    df=ak.stock_financial_report_sina(stock=symbol,symbol=name)
                    result['financials'][symbol][name]=json.loads(df[(df['报告日'].astype(str).str.endswith('1231'))].head(4).to_json(orient='records',force_ascii=False))
                except Exception as exc:result['financials'][symbol][name]={'error':type(exc).__name__}
        try:
            sec=requests.get('https://data.sec.gov/api/xbrl/companyfacts/CIK0000789019.json',headers={'User-Agent':'Sequoia-X research admin@geneelias.com'},timeout=15)
            result['sec_status']=sec.status_code
            if sec.ok:
                facts=sec.json()['facts']['us-gaap']
                result['sec_tags']={k:{'units':{unit:values[-12:] for unit,values in v.get('units',{}).items()}} for k,v in facts.items() if any(term in k.lower() for term in ['netcashprovidedbyusedinoperat','paymentstoacquireproperty','commonstocksharesoutstanding','longtermdebt','shortterm','commercialpaper','financelease','interestexpense','sharebasedcompensation','cashcashequivalents'])}
        except Exception as exc:result['sec_error']=type(exc).__name__
        headers={'Authorization':'Bearer '+os.environ['GH_TOKEN'],'Accept':'application/vnd.github+json'}
        base='https://api.github.com/repos/'+os.environ['GITHUB_REPOSITORY']
        response=requests.get(base+'/actions/workflows/research-schedule.yml/runs',params={'per_page':16},headers=headers,timeout=15)
        result['runs_status']=response.status_code
        if response.ok:
            for run in response.json().get('workflow_runs',[]):
                row={k:run.get(k) for k in ['id','event','status','conclusion','created_at','run_started_at','updated_at']}
                jobs=requests.get(base+'/actions/runs/'+str(run['id'])+'/jobs',headers=headers,timeout=15)
                if jobs.ok:row['jobs']=[{k:j.get(k) for k in ['started_at','completed_at','conclusion','steps']} for j in jobs.json().get('jobs',[])]
                result['runs'].append(row)
    (root/'_state/diagnostics.json').write_text(json.dumps(result,ensure_ascii=False,default=str))
    state.save()
    print('Read-only diagnostics persisted encrypted; no orders or messages created.')

if __name__=='__main__':main()
