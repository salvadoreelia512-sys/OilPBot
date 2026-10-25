"""Free, cached evidence and reproducible joint Monte Carlo scenarios.
News changes uncertainty, not a fabricated bullish/bearish prediction.
"""
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
import math
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np

from src.config import BotSettings
from src.news import RawHeadline, NewsRiskAdjuster

SOURCES = {'BBC World': 'https://feeds.bbci.co.uk/news/world/rss.xml',
           'EIA energy': 'https://www.eia.gov/rss/todayinenergy.xml'}
_lock = threading.Lock()
_cache = None
_until = 0


def news_context(now):
    global _cache, _until
    with _lock:
        if time.monotonic() >= _until:
            def fetch(item):
                name, url = item
                try:
                    r = httpx.get(url, timeout=4, follow_redirects=True)
                    r.raise_for_status()
                    items = []
                    for node in ET.fromstring(r.text).findall('.//item'):
                        try:
                            published = parsedate_to_datetime(node.findtext('pubDate') or '').astimezone(timezone.utc)
                        except (ValueError, TypeError, AttributeError):
                            continue
                        items.append(RawHeadline(title=node.findtext('title') or '', source=name, published_utc=published,
                                                 url=node.findtext('link') or '', summary=node.findtext('description') or ''))
                    return name, [h for h in items if 0 <= (now-h.published_utc).total_seconds() <= 86400
                                  and any(k in (h.title+' '+h.summary).lower() for k in
                                          ['oil','crude','opec','hormuz','iran','tanker','refinery','sanction','pipeline','petroleum'])], True
                except Exception:
                    previous = next((items for source, items, _ in (_cache or []) if source == name), [])
                    return name, previous, False
            with ThreadPoolExecutor(max_workers=2) as pool:
                fetched = list(pool.map(fetch, SOURCES.items()))
            _cache = fetched
            _until = time.monotonic()+600
        adjuster = NewsRiskAdjuster()
        headlines = [h for _, items, _ in _cache for h in items if 0 <= (now-h.published_utc).total_seconds() <= 86400]
        adjuster.ingest_headlines(headlines)
        adjustment = adjuster.compute_adjustment(now)
        return {'sources': [{'name':name,'url':SOURCES[name],'available':ok} for name,_,ok in _cache],
                'coverage':sum(ok for _,_,ok in _cache),
                'drivers':[{'title':d.title,'event_type':d.event_type,'impact':d.effective_impact} for d in adjustment.drivers[:5]],
                'headlines':[{'title':h.title,'url':h.url,'published_utc':h.published_utc.isoformat()} for h in headlines[:8]],
                'vol_multiplier':adjustment.vol_multiplier, 'jump_intensity':adjustment.jump_intensity_annual,
                'method':'Rule-based clustering, scoring and time decay; limited free-feed coverage, not global understanding or sentiment certainty.'}


def history(snapshot):
    from src.storage import record_quote
    return record_quote(snapshot)


def enhance(result, snapshot, request, now, context=None, observations=None):
    from src.forecast import trading_hours
    context = news_context(now) if context is None else context
    observations = history(snapshot) if observations is None else observations
    returns, durations = [], []
    for (t0,p0),(t1,p1) in zip(observations, observations[1:]):
        hours=trading_hours(datetime.fromtimestamp(t0,timezone.utc),datetime.fromtimestamp(t1,timezone.utc))
        if hours>0:
            returns.append(math.log(p1/p0));durations.append(hours/(252*23))
    mature=len(returns)>=20 and sum(durations)*(252*23)>=4
    sigma=result['model']['annual_volatility']
    drift=0.; direction='wait'
    if mature:
        variance=float(np.mean(np.square(returns)/np.array(durations)))
        empirical=max(.10,min(1.5,math.sqrt(variance)))
        sigma=.7*empirical+.3*sigma
        raw=sum(returns)/sum(durations)
        se=sigma/math.sqrt(sum(durations))
        if abs(raw)>1.96*se:
            drift=float(np.clip(raw*.2,-.25,.25))
            direction='long' if drift>0 else 'short'
    sigma*=min(2.25, max(1.,context['vol_multiplier']))
    jumps=min(65.,max(0.,context['jump_intensity']))
    # Stable common random draws eliminate fresh Monte Carlo noise on identical evidence.
    rng=np.random.default_rng(20261025)  # Common random numbers across evidence updates.
    month_end=now+timedelta(days=30)
    result['horizons'].append({'label':'1 month','hours':720,'trading_hours':round(trading_hours(now,month_end),3),'ends_at_utc':month_end.isoformat()})
    n=12000
    paths=np.full(n,math.log(snapshot.price));touched=np.zeros(n,dtype=bool)
    projection=[]; probabilities={};endings={h:label for label,h in [(r['label'],r['hours']) for r in result['horizons']]}
    delay=trading_hours(snapshot.timestamp,now)/(252*23)
    if delay:
        paths+=(drift-.5*sigma*sigma)*delay+sigma*math.sqrt(delay)*rng.standard_normal(n)
    log_target=math.log(request.target_price)
    for hour in range(721):
        if hour:
            dt=trading_hours(now+timedelta(hours=hour-1),now+timedelta(hours=hour))/(252*23)
            previous=paths.copy()
            if dt:
                counts=rng.poisson(jumps*dt,n)
                k=math.exp(.5*.025**2)-1
                paths+=(drift-.5*sigma*sigma-jumps*k)*dt+sigma*math.sqrt(dt)*rng.standard_normal(n)+np.sqrt(counts)*.025*rng.standard_normal(n)
                a=log_target-previous if request.direction=='long' else previous-log_target
                b=log_target-paths if request.direction=='long' else paths-log_target
                crossed=(a<=0)|(b<=0)
                bridge=np.exp(np.minimum(0,-2*np.maximum(a,0)*np.maximum(b,0)/(sigma*sigma*dt)))
                touched|=crossed|(rng.random(n)<bridge)
        else:
            touched|=paths>=log_target if request.direction=='long' else paths<=log_target
        prices=np.exp(paths);low,median,high=np.quantile(prices,[.1,.5,.9])
        projection.append({'hours':hour,'low':float(low),'median':float(median),'high':float(high)})
        if hour in endings: probabilities[hour]=float(np.mean(touched))
    for row in result['horizons']:
        row['probability']=probabilities[row['hours']]
        row['sampling_error_pp']=round(1.96*math.sqrt(row['probability']*(1-row['probability'])/n)*100,2)
    result['projection']=projection
    can_suggest=mature and context['coverage']==len(SOURCES) and result['quote']['market_open'] and direction!='wait'
    target=snapshot.price*math.exp((1 if direction=='long' else -1)*sigma*math.sqrt(115/(252*23))*.5) if can_suggest else None
    wait_reasons=[]
    if not mature: wait_reasons.append(f'History warming up ({len(observations)} observed quotes; need 20 return intervals across four trading hours)')
    if context['coverage']<len(SOURCES): wait_reasons.append('news coverage incomplete')
    if not result['quote']['market_open']: wait_reasons.append('market closed')
    if direction=='wait': wait_reasons.append('momentum inconclusive')
    result['outlook']={'action':direction if can_suggest else 'wait','horizon':'1 week' if can_suggest else None,
                       'target_price':target,'leverage':1,
                       'reason':'Momentum exceeds its sampling uncertainty; experimental outlook, not a validated trading edge.' if can_suggest else '; '.join(wait_reasons)+'.',
                       'risk_note':'1× unleveraged reference only; no personalized leverage without loss limits and liquidation modeling.'}
    result['evidence']={'observations':len(observations),'history_ready':mature,'news':context,
                       'validation':'Uncalibrated: no measured out-of-sample accuracy is claimed. News affects volatility and jump risk, not price direction.',
                       'unmodeled':'Live inventories, USD/rates, futures curve, positioning, AIS, holidays and execution costs are not integrated.'}
    result['model'].update({'annual_volatility':sigma,'basis':f'12,000-path jump-diffusion Monte Carlo · history + decayed news risk · drift {drift:.3f}',
                            'interval':'10th / 50th / 90th percentile scenarios; not worst/best bounds or individual simulated paths',
                            'assumptions':'Common paths across horizons; Brownian-bridge touch correction (approximate with jumps); regular WTI schedule; uncalibrated.',
                            'sampling_note':'Intervals quantify simulation noise only, not model error or forecast accuracy.'})
    return result
