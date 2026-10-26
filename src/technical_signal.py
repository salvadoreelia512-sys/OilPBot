"""Input-independent daily/weekly technical agreement and conditional breakout levels.
Ratings are correlated indicator votes, not probabilities or validated advice.
"""
import hashlib
import json
import math
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from src.strategic import data_path, load_data, refresh_if_needed

RULE_VERSION='technical-breakout-v1'
REPORT_PATH=Path(__file__).parent/'data/signal_validation.json'
NY=ZoneInfo('America/New_York')


def ema(values,period):
    result=float(values[0]);alpha=2/(period+1)
    for value in values[1:]:result=alpha*float(value)+(1-alpha)*result
    return result


def indicators(bars,periods):
    closes=np.array([b['close'] for b in bars],dtype=float)
    if len(closes)<max(max(periods),35):raise ValueError('Insufficient closed-bar history')
    votes=[]
    def add(name,value,comparison):
        votes.append({'name':name,'value':float(value),'vote':int(comparison)})
    last=closes[-1]
    for period in periods:
        simple=float(np.mean(closes[-period:]));exponential=ema(closes,period)
        add('SMA '+str(period),simple,np.sign(last-simple));add('EMA '+str(period),exponential,np.sign(last-exponential))
    changes=np.diff(closes);gain=float(np.maximum(changes[:14],0).mean());loss=float(np.maximum(-changes[:14],0).mean())
    for change in changes[14:]:gain=(gain*13+max(change,0))/14;loss=(loss*13+max(-change,0))/14
    rsi=100*gain/(gain+loss) if gain+loss>0 else 50.
    add('RSI 14',rsi,1 if rsi<30 else -1 if rsi>70 else 0)
    fast=slow=float(closes[0]);signal=0.;macd=0.
    for close in closes[1:]:
        fast=2/13*close+11/13*fast;slow=2/27*close+25/27*slow
        macd=fast-slow;signal=2/10*macd+8/10*signal
    add('MACD 12/26/9',macd-signal,np.sign(macd-signal))
    counts={name:sum(v['vote']==code for v in votes) for name,code in [('buy',1),('neutral',0),('sell',-1)]}
    return {'indicators':votes,'counts':counts,'score':sum(v['vote'] for v in votes)/len(votes)}


def weekly_bars(bars,asof):
    groups={}
    for bar in bars:
        day=date.fromisoformat(bar['date']);monday=day-timedelta(days=day.weekday())
        if monday+timedelta(days=4)>=asof:continue  # Completed regular Friday session only.
        if monday not in groups:groups[monday]=dict(bar)
        else:
            row=groups[monday];row.update({'date':bar['date'],'high':max(row['high'],bar['high']),
                                         'low':min(row['low'],bar['low']),'close':bar['close']})
    return list(groups.values())


def next_session(day):
    day=day+timedelta(days=1)
    while day.weekday()>4:day+=timedelta(days=1)
    return day


def technical_setup(bars,asof):
    known=[b for b in bars if b['date']<asof.isoformat()]
    if len(known)<253:raise ValueError('Daily history warming up')
    daily=indicators(known,(20,50,200));weeks=weekly_bars(known,asof)
    weekly=indicators(weeks,(10,20,50));score=(daily['score']+weekly['score'])/2
    direction='long' if score>=.25 else 'short' if score<=-.25 else None
    true_ranges=[max(b['high']-b['low'],abs(b['high']-a['close']),abs(b['low']-a['close'])) for a,b in zip(known,known[1:])]
    atr=float(np.mean(true_ranges[:14]))
    for value in true_ranges[14:]:atr=(atr*13+value)/14
    if not math.isfinite(atr) or atr<=0:raise ValueError('Invalid ATR')
    high=max(b['high'] for b in known[-20:]);low=min(b['low'] for b in known[-20:])
    counts={key:daily['counts'][key]+weekly['counts'][key] for key in ('buy','neutral','sell')}
    rating='Strong buy' if score>=.5 else 'Buy' if score>=.15 else 'Strong sell' if score<=-.5 else 'Sell' if score<=-.15 else 'Neutral'
    levels={}
    for side,sign in [('long',1),('short',-1)]:
        entry=high+.1*atr if sign>0 else low-.1*atr
        if min(entry-sign*1.5*atr,entry+sign*3*atr)<=0:continue
        levels[side]={'direction':side,'entry':entry,'invalidation':entry-sign*1.5*atr,
                      'week_target':entry+sign*2*atr,'month_target':entry+sign*3*atr}
    if direction not in levels:direction=None
    return {'rule_version':RULE_VERSION,'as_of_bar':known[-1]['date'],'score':score,'rating':rating,
            'counts':counts,'daily':daily,'weekly':weekly,'weekly_as_of':weeks[-1]['date'],
            'atr_14':atr,'direction':direction,'levels':levels,
            'valid_until':datetime.combine(next_session(date.fromisoformat(known[-1]['date'])),time(17),NY).astimezone(timezone.utc).isoformat()}


@lru_cache(maxsize=4)
def cached_setup(fingerprint,asof,rows):
    bars=[dict(zip(('date','open','high','low','close'),row)) for row in rows]
    return technical_setup(bars,date.fromisoformat(asof))


@lru_cache(maxsize=2)
def validation_report(modified):
    return json.loads(REPORT_PATH.read_text())


def live_signal(snapshot,quote,news,now):
    path=data_path();raw=path.read_bytes();fingerprint=hashlib.sha256(raw).hexdigest()
    data=json.loads(raw)
    rows=tuple((b['date'],b['open'],b['high'],b['low'],b['close']) for b in data['bars'])
    local=now.astimezone(NY)
    cutoff=local.date()+timedelta(days=1) if local.hour>=17 else local.date()
    setup=cached_setup(fingerprint,cutoff.isoformat(),rows)
    refresh_if_needed(now+timedelta(days=1) if local.hour>=17 else now,setup['as_of_bar'])
    report=validation_report(REPORT_PATH.stat().st_mtime_ns) if REPORT_PATH.exists() else {}
    matches=report.get('data_sha256')==fingerprint and report.get('rule_version')==RULE_VERSION
    selected=setup['levels'].get(setup['direction'])
    blocked=[]
    if not (matches and report.get('reliability_gate_passed')):blocked.append('No positive historical edge established; watch only')
    if not quote['usable']:blocked.append('The live quote is stale')
    if not quote['market_open']:blocked.append('Market closed')
    if (now.date()-date.fromisoformat(setup['as_of_bar'])).days>10:blocked.append('Daily history is stale')
    if now>=datetime.fromisoformat(setup['valid_until']):blocked.append('Setup expired; waiting for a newer completed session')
    if news['coverage']<len(news['sources']):blocked.append('News coverage incomplete')
    if news['vol_multiplier']>=1.75 or news['jump_intensity']>=40:blocked.append('Elevated headline risk; stand aside')
    state='waiting_for_trigger'
    if selected:
        sign=1 if selected['direction']=='long' else -1
        trigger_seen=snapshot.high>=selected['entry'] if sign>0 else snapshot.low<=selected['entry']
        stop_seen=snapshot.low<=selected['invalidation'] if sign>0 else snapshot.high>=selected['invalidation']
        target_seen=snapshot.high>=selected['week_target'] if sign>0 else snapshot.low<=selected['week_target']
        if trigger_seen and stop_seen:blocked.append('Entry and invalidation both seen this session; intrabar order unknown')
        if target_seen:blocked.append('Reference target already seen; wait for a new setup')
        if trigger_seen and sign*(snapshot.price-selected['entry'])<0:blocked.append('Trigger occurred earlier this session; no fresh entry')
        if sign*(snapshot.price-selected['entry'])>=0:
            state='trigger_crossed'
            if sign*(snapshot.price-selected['entry'])>.25*setup['atr_14']:
                state='entry_passed';blocked.append('Entry reference has passed; do not chase')
    else:blocked.append('Daily and weekly trend agreement is insufficient')
    return {**setup,'quote':quote,'setup':selected,'state':state,'status':'stand_aside' if blocked else 'conditional_watch',
            'blocked_reasons':blocked,'generated_at_utc':now.isoformat(),'data_sha256':fingerprint,
            'historical_validation':{**report,'current_archive_matches':matches,'reliability_gate_passed':bool(matches and report.get('reliability_gate_passed'))} if report.get('rule_version')==RULE_VERSION else {'reliability_gate_passed':False,'reason':'No matching historical rule report'},
            'news':{'coverage':news['coverage'],'sources':len(news['sources']),'risk_scale':news['vol_multiplier'],
                    'headlines':news['headlines'][:3],'directional_effect_validated':False},
            'notice':'Conditional daily/weekly breakout setup. Indicator agreement is not a winning probability. News is a risk filter, not a proven directional predictor. No orders are placed.',
            'levels_basis':'20 completed daily high/low breakout; 14-day ATR buffer, 1.5 ATR invalidation, 2/3 ATR alternative week/month targets. Regular sessions only; holiday exceptions may differ.'}
