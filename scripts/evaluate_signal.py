"""Fixed technical rule evaluation; no fitting or tuning on the later sample."""
import hashlib,json,math,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from datetime import date,timedelta
import numpy as np
from src.strategic import DATA_PATH
from src.technical_signal import RULE_VERSION,REPORT_PATH,technical_setup,next_session


def trade(setup,future,horizon):
    side=setup['direction'];levels=setup['levels'].get(side)
    if not levels:return None
    bar=future[0]
    if date.fromisoformat(bar['date'])!=next_session(date.fromisoformat(setup['as_of_bar'])):return None
    sign=1 if side=='long' else -1;entry=levels['entry'];stop=levels['invalidation'];target=levels['week_target' if horizon==5 else 'month_target']
    if (bar['high']<entry if sign>0 else bar['low']>entry):return None
    fill=max(entry,bar['open']) if sign>0 else min(entry,bar['open'])
    if sign*(target-fill)<=0:return None  # Gap has already passed the target; no fresh entry.
    outcome='time_exit';exit_price=future[-1]['close'];ambiguous=False
    for offset,b in enumerate(future):
        stop_hit=b['low']<=stop if sign>0 else b['high']>=stop
        target_hit=b['high']>=target if sign>0 else b['low']<=target
        if stop_hit:
            exit_price=(min(stop,b['open']) if sign>0 else max(stop,b['open'])) if offset else stop
            outcome='stop';ambiguous=target_hit or offset==0;break
        if target_hit:exit_price=target;outcome='target';break
    return {'gross_return':sign*(exit_price-fill)/fill,'outcome':outcome,'ambiguous':ambiguous,'direction':side}


def metrics(trades):
    if not trades:return {'trades':0,'gate_passed':False}
    gross=np.array([t['gross_return'] for t in trades]);rng=np.random.default_rng(20261026);n=len(gross)
    samples=[]
    for _ in range(2000):
        starts=rng.integers(0,n,size=math.ceil(n/6));indices=np.concatenate([(s+np.arange(6))%n for s in starts])[:n]
        samples.append(float((gross[indices]-.0025).mean()))
    interval=np.quantile(samples,[.025,.975]).tolist()
    return {'trades':n,'mean_gross_return':float(gross.mean()),'net_mean_by_roundtrip_bps':{str(c):float((gross-c/10000).mean()) for c in (5,10,25)},
            'net_positive_fraction_at_25bps':float(np.mean(gross>.0025)),'mean_net_return_95pct_block_interval_at_25bps':interval,
            'conservative_ambiguous_trades':sum(t['ambiguous'] for t in trades),
            'directions':{s:sum(t['direction']==s for t in trades) for s in ('long','short')},
            'gate_passed':bool(n>=50 and interval[0]>0)}


def evaluate(data):
    bars=data['bars'];out={}
    for horizon in (5,21):
        development=[];heldout=[];origins=0;eligible=0
        for i in range(504,len(bars)-horizon,horizon):
            origin=bars[i];future=bars[i+1:i+horizon+1];origins+=int(origin['date']>='2019-01-01')
            setup=technical_setup(bars[:i+1],date.fromisoformat(origin['date'])+timedelta(days=1))
            if not setup['direction']:continue
            eligible+=int(origin['date']>='2019-01-01')
            result=trade(setup,future,horizon)
            if result is None:continue
            result['origin']=origin['date']
            if future[-1]['date']<'2019-01-01':development.append(result)
            elif origin['date']>='2019-01-01':heldout.append(result)
        out['1 week' if horizon==5 else '1 month']={'tested_origins':origins,'directional_origins':eligible,
                    'development':metrics(development),'heldout':metrics(heldout)}
    return {'rule_version':RULE_VERSION,'data_last_bar':bars[-1]['date'],'instrument':'TVC:USOIL','data_sha256':hashlib.sha256(DATA_PATH.read_bytes()).hexdigest(),
            'development_end_exclusive':'2019-01-01','heldout_start':'2019-01-01','horizons':out,
            'reliability_gate_passed':all(v['heldout']['gate_passed'] for v in out.values()),
            'method':'Fixed daily/weekly indicator consensus; next-session 20-day breakout entry; 1.5 ATR stop, 2/3 ATR targets, 5/21-session exits. Non-overlapping windows, gaps at worse opening fills, ambiguous daily order resolved stop-first. Six-trade blocks; no rule tuning on held-out data.',
            'limitations':['Historical daily OHLC cannot establish intrabar order; ambiguous entries/stops treated as losses.',
               'Assumed 5/10/25 bps costs exclude actual broker spreads, financing and execution differences.',
               'News filter and live intraday trigger timing are not included in this historical technical-rule test.',
               'Positive historical performance does not prove the best action or future reliability.',
               'Daily OHLC data can be revised; no point-in-time release vintage is available.']}


if __name__=='__main__':
    result=evaluate(json.loads(DATA_PATH.read_text()));REPORT_PATH.write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    print('GATE',result['reliability_gate_passed'])
    for h,v in result['horizons'].items():print(h,v['tested_origins'],v['heldout'])
