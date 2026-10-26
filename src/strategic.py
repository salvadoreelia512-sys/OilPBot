"""Causal daily-regime inputs and an explicit historical daily-origin validation gate.
No headline, inventory number, or trend is assumed to imply a profitable direction.
"""
import hashlib
import json
import math
import threading
import time
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import numpy as np

DATA_PATH = Path(__file__).parent/'data/strategic.json'
REPORT_PATH = Path(__file__).parent/'data/strategic_validation.json'
CACHE_PATH = Path('/tmp/oilpbot-strategic-current.json')
CACHE_REPORT = Path('/tmp/oilpbot-strategic-validation.json')
_refresh_lock = threading.Lock()
_refresh_attempt = 0.
_refresh_error = None
SPEC_VERSION = 'strategic-tvc-v1'
YEAR_HOURS = 252 * 23


@lru_cache(maxsize=4)
def read_json(path, modified):
    return json.loads(Path(path).read_text())


def data_path():
    if CACHE_PATH.exists() and CACHE_REPORT.exists():
        try:
            cached=read_json(str(CACHE_PATH),CACHE_PATH.stat().st_mtime_ns)
            bundled=read_json(str(DATA_PATH),DATA_PATH.stat().st_mtime_ns)
            if cached.get('instrument')=='TVC:USOIL' and cached['bars'][-1]['date']>=bundled['bars'][-1]['date']:
                return CACHE_PATH
        except (OSError,ValueError,KeyError,IndexError,TypeError):
            pass
    return DATA_PATH


def load_data():
    path=data_path()
    return read_json(str(path),path.stat().st_mtime_ns)


def load_validation():
    path=CACHE_REPORT if data_path()==CACHE_PATH else REPORT_PATH
    return read_json(str(path),path.stat().st_mtime_ns) if path.exists() else {'promoted':False,'selected_model':'baseline','horizons':{}}


def refresh_if_needed(now, last_bar):
    global _refresh_attempt
    lag=int(np.busday_count(date.fromisoformat(last_bar)+timedelta(days=1),now.date()))
    if lag<1 or (_refresh_attempt and time.monotonic()-_refresh_attempt<12*3600) or not _refresh_lock.acquire(blocking=False):
        return
    _refresh_attempt=time.monotonic()
    def worker():
        global _refresh_error
        try:
            from scripts.build_strategic_data import build
            from scripts.evaluate_strategic import evaluate
            temporary=CACHE_PATH.with_suffix('.json.tmp')
            data=build(refresh=True,destination=temporary)
            report=evaluate(data,hashlib.sha256(temporary.read_bytes()).hexdigest())
            # A daily data refresh cannot promote an unreviewed new model family.
            report.update({'promoted':False,'selected_model':'baseline','candidate_requires_review':True})
            report_tmp=CACHE_REPORT.with_suffix('.json.tmp')
            report_tmp.write_text(json.dumps(report,allow_nan=False))
            temporary.replace(CACHE_PATH);report_tmp.replace(CACHE_REPORT)
            _refresh_error=None
        except Exception:
            _refresh_error='Public data refresh unavailable; retaining dated validated archive'
        finally:
            _refresh_lock.release()
    threading.Thread(target=worker,name='strategic-refresh',daemon=True).start()


def parameters(bars, weekly, asof):
    """Use only daily closes and conservatively available weekly releases."""
    known = [b for b in bars if b['date'] <= asof]
    closes = np.array([b['close'] for b in known],dtype=float)
    if len(closes)<253:
        raise ValueError('At least 253 daily bars required')
    positive=(closes[1:]>0)&(closes[:-1]>0)
    returns=np.log(closes[1:][positive]/closes[:-1][positive])
    recent=returns[-60:];weights=.97**np.arange(len(recent))[::-1]
    short=float(np.average(recent**2,weights=weights)*252)
    long=float(np.mean(returns[-252:]**2)*252)
    def available(name):
        return [r for r in weekly.get(name,[]) if (date.fromisoformat(r['date'])+timedelta(days=7)).isoformat()<=asof]
    inventories,production=available('inventories'),available('production')
    fundamentals={}
    pressure=1.
    if len(inventories)>=54:
        vals=np.array([r['value'] for r in inventories[-54:]])
        scale=float(np.std(np.diff(vals[:-1]),ddof=1))
        shock=float((vals[-1]-vals[-2])/scale) if scale>0 else 0.
        pressure+=min(.15,max(0,abs(shock)-1)*.05)
        fundamentals['inventories']={'week_ending':inventories[-1]['date'],'available_after':(date.fromisoformat(inventories[-1]['date'])+timedelta(days=7)).isoformat(),
                                     'thousand_barrels':float(vals[-1]),'weekly_change':float(vals[-1]-vals[-2]),'change_z':shock,
                                     'year_on_year_percent':float((vals[-1]/vals[-53]-1)*100)}
    if len(production)>=53:
        current,previous=production[-1]['value'],production[-5]['value']
        change=current/previous-1
        pressure+=min(.10,abs(change)*2)
        fundamentals['production']={'week_ending':production[-1]['date'],'thousand_barrels_per_day':current,
                                    'four_week_change_percent':change*100,'year_on_year_percent':(current/production[-53]['value']-1)*100}
    return {'short_variance':float(np.clip(short,.1**2,3.**2)), 'long_variance':float(np.clip(long,.1**2,3.**2)),
            'fundamental_scale':pressure,'fundamentals':fundamentals,'last_bar':known[-1]['date'],
            'annual_volatility_60d':math.sqrt(short),'annual_volatility_252d':math.sqrt(long),
            'momentum_20d_percent':float((closes[-1]/closes[-21]-1)*100) if closes[-21]>0 and closes[-1]>0 else None,
            'negative_price_bars':sum(b['low']<=0 for b in known),'observations':len(known)}


def integrated_variance(state, trading_years, model):
    if trading_years<=0:return 0.
    short,long=state['short_variance'],state['long_variance']
    if model=='baseline':return short*trading_years
    # Twenty trading-day half-life for the current daily volatility regime.
    tau=(20/252)/math.log(2)
    total=long*trading_years+(short-long)*tau*(-math.expm1(-trading_years/tau))
    return total*(state['fundamental_scale']**2 if model=='regime_fundamentals' else 1)


def context(now):
    data=load_data();report=load_validation()
    state=parameters(data['bars'],data['weekly'],(now.date()-timedelta(days=1)).isoformat())
    fingerprint=hashlib.sha256(data_path().read_bytes()).hexdigest()
    matches=report.get('data_sha256')==fingerprint and report.get('version')==SPEC_VERSION
    # Never silently reuse a validation report after refreshing its input archive.
    promoted=bool(matches and report.get('promoted'))
    selected=report.get('selected_model','baseline') if promoted else 'baseline'
    refresh_if_needed(now,state['last_bar'])
    age=(now.date()-date.fromisoformat(state['last_bar'])).days
    if age>7:promoted=False;selected='baseline'
    return {**state,'data_sha256':fingerprint,'model':selected,'proxy_validation_promoted':promoted,'historical_upgrade_promoted':promoted,'data_age_days':age,
            'data_status':'current completed TVC daily history' if age<=4 else 'stale TVC daily history',
            'refresh_in_progress':_refresh_lock.locked(),'refresh_error':_refresh_error,
            'validation':report if matches else {'promoted':False,'reason':'Validation fingerprint or specification does not match archive'},
            'sources':data['sources'],'limitations':data['limitations'],
            'coverage_gaps':['No point-in-time historical headline archive','No globally complete OPEC production, demand or positioning feed',
                             'Daily-origin validation does not establish arbitrary live intraday-origin target-touch calibration', 'No futures-curve or term-structure model'],
            'directional_edge_validated':False}
