"""Read-only fixed public-symbol probes. Save metadata only, never prices/holdings."""
import contextlib
from datetime import datetime,timezone
import json,math,os,sys,time
from pathlib import Path

upstream=Path(sys.argv[1]).resolve();output=Path(sys.argv[2])
sys.path.insert(0,str(upstream/'.agents/skills/data-access/scripts'))
import common
# Preserve the executor's network route even if the upstream wants a direct retry.
original_session=common._em_session
common._em_session=lambda trust_env:original_session(True)
from sources.yahoo import yahoo_kline
from sources.astock_ported import tencent_kline
from sources.eastmoney import financial_statements_global
from sources.global_research import global_financials_pit


def summarize(rows,kind):
    if hasattr(rows,'to_dict'):rows=rows.to_dict(orient='records')
    if isinstance(rows,dict):
        info={k:rows.get(k) for k in ['dropped_after_as_of','dropped_no_notice','dropped_too_old','fetched_rows'] if k in rows}
        rows=rows.get('rows',[])
    else:info={}
    info['row_count']=len(rows)
    info['field_names']=sorted({str(k) for row in rows for k in row})
    info['dates']=sorted({str(row.get('date') or row.get('REPORT_DATE') or row.get('end') or '')[:10] for row in rows if row.get('date') or row.get('REPORT_DATE') or row.get('end')})[-6:]
    info['currency_tags']=sorted({str(row[k]) for row in rows for k in ['currency','CURRENCY'] if row.get(k)})
    if kind=='kline':
        invalid=0
        for row in rows:
            try:
                values={k:float(row[k]) for k in ['open','high','low','close','volume']}
                if not all(math.isfinite(x) for x in values.values()) or min(values[k] for k in ['open','high','low','close'])<=0 or values['volume']<0 or values['high']<max(values['open'],values['close'],values['low']) or values['low']>min(values['open'],values['close'],values['high']):invalid+=1
            except (KeyError,TypeError,ValueError):invalid+=1
        info['invalid_ohlcv_rows']=invalid
    else:
        info['item_labels']=sorted({str(row.get('ITEM_NAME')) for row in rows if row.get('ITEM_NAME')})[:18]
    return info


def main():
    cases=[
        ('CN_tencent_kline','CN','600519','kline',lambda:tencent_kline('sh600519',count=160,adjust='qfq')),
        ('US_yahoo_kline','US','AAPL','kline',lambda:yahoo_kline('AAPL','US',range_='1y')),
        ('HK_yahoo_kline','HK','00700','kline',lambda:yahoo_kline('00700','HK',range_='1y')),
        ('US_eastmoney_balance','US','AAPL','financial',lambda:financial_statements_global('AAPL','US','balance',page_size=50)),
        ('US_eastmoney_cashflow','US','AAPL','financial',lambda:financial_statements_global('AAPL','US','cashflow',page_size=50)),
        ('HK_eastmoney_balance','HK','00700','financial',lambda:financial_statements_global('00700','HK','balance',page_size=50)),
        ('US_financials_pit','US','AAPL','financial',lambda:global_financials_pit('AAPL','US',as_of='2026-10-30',page_size=30)),
        ('HK_financials_pit','HK','00700','financial',lambda:global_financials_pit('00700','HK',as_of='2026-10-30',page_size=30)),
    ]
    result={'review':'Vibe-Research public endpoint connectivity only','upstream_commit':'84c141b86b5e7540b29f5f4cd5457dc0ebd6052e','generated_at':datetime.now(timezone.utc).isoformat(),'run_id':os.environ.get('GITHUB_RUN_ID'),'no_private_inputs':True,'production_modified':False,'cases':[]}
    for name,market,symbol,kind,fn in cases:
        row={'test':name,'market':market,'fixed_public_test_symbol':symbol};start=time.monotonic()
        try:
            with open(os.devnull,'w') as sink,contextlib.redirect_stdout(sink),contextlib.redirect_stderr(sink):data=fn()
            row.update(summarize(data,kind));row['status']='returned_rows' if row['row_count'] else 'empty'
        except Exception as exc:
            row['status']='fetch_error';row['error_type']=type(exc).__name__
            response=getattr(exc,'response',None)
            if response is not None:row['http_status']=response.status_code
        row['seconds']=round(time.monotonic()-start,2);result['cases'].append(row)
    output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))
    print('Public endpoint probes complete; only schema/status metadata saved.')

if __name__=='__main__':main()
