"""Build a small auditable historical context archive from already downloaded public files.
Inputs: EIA RWTCd.xls, Yahoo ^GSPC daily CSV, Iacoviello daily GPR XLS.
"""
import hashlib,json
from pathlib import Path
import numpy as np
import pandas as pd

# Optional one-time builder dependencies: xlrd, yfinance, httpx.
import httpx
for path,url in [('/tmp/archive-wti','https://www.eia.gov/dnav/pet/hist_xls/RWTCd.xls'),('/tmp/historical-gpr','https://www.matteoiacoviello.com/gpr_files/data_gpr_daily_recent.xls')]:
    if not Path(path).exists():
        response=httpx.get(url,timeout=30,follow_redirects=True);response.raise_for_status();Path(path).write_bytes(response.content)
if not Path('/tmp/sp500.csv').exists():
    import yfinance as yf
    prices=yf.Ticker('^GSPC').history(start='2024-12-01',end='2026-10-26',interval='1d',raise_errors=True)
    if prices.empty:raise ValueError('S&P 500 history unavailable; do not build a partial archive')
    prices.to_csv('/tmp/sp500.csv')
start=pd.Timestamp('2024-12-01')
w=pd.read_excel('/tmp/archive-wti',sheet_name=1,header=2).iloc[:,:2];w.columns=['date','wti'];w['date']=pd.to_datetime(w['date']);w=w[w.date>=start].dropna()
s=pd.read_csv('/tmp/sp500.csv');s['date']=pd.to_datetime(s.iloc[:,0],utc=True).dt.tz_localize(None).dt.normalize();s=s[['date','Close']].rename(columns={'Close':'sp500'})
g=pd.read_excel('/tmp/historical-gpr')[['date','GPRD']].rename(columns={'GPRD':'gpr'});g['date']=pd.to_datetime(g.date);g=g[g.date>=start].dropna()
series={name:[{'date':row.date.strftime('%Y-%m-%d'),'value':float(getattr(row,name))} for row in frame.itertuples() if np.isfinite(getattr(row,name))] for name,frame in [('wti',w),('sp500',s),('gpr',g)]}
# Conservative seven-calendar-day lag for revised/published oil and GPR context.
x=w.sort_values('date').copy();x['ret']=np.log(x.wti).diff();x['momentum']=x.ret.rolling(5).mean().shift(1);x['abs_lag']=x.ret.abs().shift(1)
s['stock_ret']=np.log(s.sp500).diff();s['available']=s.date+pd.Timedelta(days=1)
g['available']=g.date+pd.Timedelta(days=7);g['risk']=np.log1p(g.gpr)
x=pd.merge_asof(x,s[['available','stock_ret']].sort_values('available'),left_on='date',right_on='available',direction='backward').drop(columns='available')
x=pd.merge_asof(x,g[['available','risk']].sort_values('available'),left_on='date',right_on='available',direction='backward').dropna()
features=['momentum','abs_lag','stock_ret','risk'];errors=[];direction=[]
for i in range(100,len(x)):
 train=x.iloc[:i];test=x.iloc[i];a=train[features].to_numpy();mean=a.mean(0);std=a.std(0)+1e-8;a=(a-mean)/std;a=np.column_stack([np.ones(len(a)),a]);v=np.r_[1,(test[features].to_numpy(dtype=float)-mean)/std]
 penalty=np.eye(5)*10;penalty[0,0]=0
 beta=np.linalg.solve(a.T@a+penalty,a.T@train.ret.to_numpy());pred=float(v@beta);actual=float(test.ret)
 errors.append(((pred-actual)**2,actual**2));direction.append(int((pred>0)==(actual>0)))
r=np.log(w.wti).diff().dropna();prior=float(np.sqrt(np.average(r.tail(60)**2,weights=np.power(.97,np.arange(len(r.tail(60)))[::-1]))*252))
report={'method':'Expanding-window ridge daily-return regression; 100 initial training rows; fit scaling on training only; 7-day GPR lag and 1-day stock lag. Revised data, not point-in-time vintages.', 'test_observations':len(errors),'model_mse':float(np.mean([e[0] for e in errors])),'zero_drift_mse':float(np.mean([e[1] for e in errors])),'direction_accuracy':float(np.mean(direction)),'continuous_touch_accuracy':None,'promoted_direction_model':False}
data={'start_requested':'2024-12-01','series':series,'annual_volatility_prior':min(1.5,max(.1,prior)),'validation':report,'sources':{'wti':'https://www.eia.gov/dnav/pet/hist_xls/RWTCd.xls','sp500':'Yahoo Finance ^GSPC daily history','gpr':'https://www.matteoiacoviello.com/gpr_files/data_gpr_daily_recent.xls'},'source_sha256':{n:hashlib.sha256(Path(p).read_bytes()).hexdigest() for n,p in [('wti','/tmp/archive-wti'),('sp500','/tmp/sp500.csv'),('gpr','/tmp/historical-gpr')]}}
Path('src/data/history.json').write_text(json.dumps(data,separators=(',',':'))+'\n')
print({k:len(v) for k,v in series.items()});print(report);print('Volatility prior:',prior)
