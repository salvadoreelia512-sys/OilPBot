"""Free, cached evidence and reproducible joint Monte Carlo scenarios.
News changes uncertainty, not a fabricated bullish/bearish prediction.
"""
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
import math
import re
from urllib.parse import urlsplit
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
           'EIA energy': 'https://www.eia.gov/rss/todayinenergy.xml',
           'OilPrice': 'https://oilprice.com/rss/main'}
_lock = threading.Lock()
_cache = None
_until = 0
_checked_at = None
_source_errors = {}
NEWS_REFRESH_SECONDS = 60
NEWS_MAX_AGE_SECONDS = 86400



def headline_priority(headline, now):
    """Potential oil relevance, not a measured price effect or direction forecast."""
    text = (headline.title + ' ' + re.sub(r'<[^>]+>', ' ', headline.summary)).lower()
    if not re.search(r'\b(oil|crude|brent|wti|opec|hormuz|petroleum|refiner\w*|tanker\w*|pipeline\w*|gasoline|diesel|energy|iran|saudi|russia)\b', text):
        return 0, 'No direct oil relevance identified'
    rules = [
        (6, r'\b(hormuz|blockade|embargo|tanker|pipeline|refiner\w*|outage|attack|missile|sanction\w*)\b', 'Potential supply or shipping disruption'),
        (5, r'\b(opec|production|output|supply|inventory|inventories|stockpiles)\b', 'Oil supply, production or inventories'),
        (4, r'\b(demand|recession|interest rates|federal reserve|inflation|china)\b', 'Potential oil demand or macroeconomic effect'),
        (2, r'\b(oil|crude|brent|wti|petroleum|gasoline|diesel|energy)\b', 'Oil and energy market developments'),
    ]
    matches = [(score, reason) for score, pattern, reason in rules if re.search(pattern, text)]
    if not matches:
        return 0, 'No direct oil relevance identified'
    score, reason = max(matches)
    age_hours = max(0, (now-headline.published_utc).total_seconds()/3600)
    return score * math.exp(-age_hours/24), reason


def headline_key(headline):
    title = re.sub(r'\s+-\s+' + re.escape(headline.source) + r'$', '', headline.title, flags=re.I)
    return ' '.join(re.findall(r'\w+', title.lower()))


def news_context(now):
    global _cache, _until, _checked_at
    with _lock:
        if time.monotonic() >= _until:
            def fetch(item):
                name, url = item
                try:
                    r = httpx.get(url, timeout=8, follow_redirects=True, headers={"User-Agent": "OilPBot/0.7", "Accept": "application/rss+xml, application/xml, text/xml"})
                    r.raise_for_status()
                    items = []
                    root = ET.fromstring(r.content)
                    if root.tag != 'rss' or root.find('channel') is None:
                        raise ValueError('Expected an RSS feed')
                    for node in root.findall('.//item'):
                        try:
                            published = parsedate_to_datetime(node.findtext('pubDate') or '')
                            if published.tzinfo is None:
                                continue
                            published = published.astimezone(timezone.utc)
                        except (ValueError, TypeError, AttributeError):
                            continue
                        title=(node.findtext('title') or '').strip()
                        url=(node.findtext('link') or '').strip()
                        parsed=urlsplit(url)
                        if not title or parsed.scheme not in ('http','https') or not parsed.hostname:
                            continue
                        items.append(RawHeadline(title=title, source=(node.findtext('source') or name).strip(), published_utc=published,
                                                 url=url, summary=node.findtext('description') or ''))
                    _source_errors.pop(name, None)
                    return name, [h for h in items if 0 <= (now-h.published_utc).total_seconds() <= NEWS_MAX_AGE_SECONDS
                                  and headline_priority(h,now)[0] > 0], True
                except Exception as exc:
                    _source_errors[name] = ('HTTP ' + str(exc.response.status_code) if isinstance(exc, httpx.HTTPStatusError)
                                            else 'Request timed out' if isinstance(exc, httpx.TimeoutException)
                                            else 'Invalid RSS' if isinstance(exc, (ET.ParseError, ValueError))
                                            else 'Connection unavailable')
                    previous = next((items for source, items, _ in (_cache or []) if source == name), [])
                    return name, previous, False
            with ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
                fetched = list(pool.map(fetch, SOURCES.items()))
            _cache = fetched
            _checked_at = datetime.now(timezone.utc)
            _until = time.monotonic() + (NEWS_REFRESH_SECONDS if all(ok for _, _, ok in fetched) else 15)
        adjuster = NewsRiskAdjuster()
        candidates = sorted([h for _, items, _ in _cache for h in items
                             if 0 <= (now-h.published_utc).total_seconds() <= NEWS_MAX_AGE_SECONDS],
                            key=lambda h: (headline_priority(h,now)[0], h.published_utc), reverse=True)
        seen = set()
        headlines = []
        for h in candidates:
            key = headline_key(h)
            if key not in seen:
                seen.add(key)
                headlines.append(h)
        adjuster.ingest_headlines(headlines)
        adjustment = adjuster.compute_adjustment(now)
        return {'sources': [{'name':name,'url':SOURCES[name],'available':ok,'error':_source_errors.get(name)} for name,_,ok in _cache],
                'coverage':sum(ok for _,_,ok in _cache),
                'drivers':[{'title':d.title,'event_type':d.event_type,'impact':d.effective_impact} for d in adjustment.drivers[:5]],
                'headlines':[{'title':h.title,'url':h.url,'source':h.source,
                              'published_utc':h.published_utc.isoformat(),
                              'used_for_risk':True, 'potential_relevance':headline_priority(h,now)[1]} for h in headlines[:8]],
                'checked_at_utc':_checked_at.isoformat() if _checked_at else None,
                'refresh_seconds':NEWS_REFRESH_SECONDS,
                'vol_multiplier':adjustment.vol_multiplier, 'jump_intensity':adjustment.jump_intensity_annual,
                'method':'Real publisher headlines from the last 24 hours, deduplicated and ranked by potential oil relevance and recency. Ranking is rule-based, not a measured price-impact prediction.'}


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
    from src.historical import load, summary
    archive=load()
    prior=archive['annual_volatility_prior']
    sigma=.8*prior+.2*result['model']['annual_volatility']
    drift=0.; direction='wait'
    if len(returns)>=5 and sum(durations)>0:
        variance=float(np.mean(np.square(returns)/np.array(durations)))
        empirical=max(.10,min(1.5,math.sqrt(variance)))
        # Gradually admit live volatility, rather than switching abruptly at warmup.
        weight=.7*min(1.,len(returns)/100,sum(durations)*(252*23)/24)
        sigma=(1-weight)*sigma+weight*empirical
    if mature:
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
        step_rng=np.random.default_rng(20261025+hour)
        jump_rng=np.random.default_rng(20271025+hour)
        if hour:
            dt=trading_hours(now+timedelta(hours=hour-1),now+timedelta(hours=hour))/(252*23)
            previous=paths.copy()
            if dt:
                counts=jump_rng.poisson(jumps*dt,n)
                k=math.exp(.5*.025**2)-1
                paths+=(drift-.5*sigma*sigma-jumps*k)*dt+sigma*math.sqrt(dt)*step_rng.standard_normal(n)+np.sqrt(counts)*.025*jump_rng.standard_normal(n)
                a=log_target-previous if request.direction=='long' else previous-log_target
                b=log_target-paths if request.direction=='long' else paths-log_target
                crossed=(a<=0)|(b<=0)
                bridge=np.exp(np.minimum(0,-2*np.maximum(a,0)*np.maximum(b,0)/(sigma*sigma*dt)))
                touched|=crossed|(step_rng.random(n)<bridge)
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
    result['historical_context']=summary(archive)
    result['outlook']['conditional_setups']=[{'direction':'long','trigger':snapshot.high*1.001,'target':snapshot.high*1.001*math.exp(sigma*math.sqrt(115/(252*23))*.5)}, {'direction':'short','trigger':snapshot.low*.999,'target':snapshot.low*.999*math.exp(-sigma*math.sqrt(115/(252*23))*.5)}]
    result['outlook']['setup_notice']='Watch-only breakout scenarios, not active entries. Targets are volatility-based, not validated recommendations.'
    setup_direction = direction if can_suggest else request.direction
    sign = 1 if setup_direction == 'long' else -1
    entry = snapshot.price if can_suggest else next(s['trigger'] for s in result['outlook']['conditional_setups'] if s['direction'] == setup_direction)
    result['outlook']['setup'] = {
        'direction': setup_direction,
        'entry_price': entry,
        'entry_condition': 'reference quote' if can_suggest else 'break above' if sign == 1 else 'break below',
        'week_target': entry * math.exp(sign * sigma * math.sqrt(trading_hours(now,now+timedelta(days=7))/(252*23)) * .5),
        'month_target': entry * math.exp(sign * sigma * math.sqrt(trading_hours(now,month_end)/(252*23)) * .5),
        'status': 'experimental signal' if can_suggest else 'conditional scenario',
        'basis': result['outlook']['reason'] if can_suggest else 'Uses your selected direction because the model has no supported directional signal. ' + result['outlook']['reason'],
        'notice': 'Entry and targets are volatility-based reference levels, not a calibrated trading recommendation. Scenario targets differ from your probability target.',
    }
    result['evidence']={'observations':len(observations),'history_ready':mature,'news':context,
                       'validation':'Uncalibrated: no measured out-of-sample accuracy is claimed. News affects volatility and jump risk, not price direction.',
                       'unmodeled':'Live inventories, USD/rates, futures curve, positioning, AIS, holidays and execution costs are not integrated.'}
    result['model'].update({'annual_volatility':sigma,'basis':f'12,000-path jump-diffusion Monte Carlo · historical WTI volatility prior + observed prices + decayed news risk · drift {drift:.3f}',
                            'interval':'10th / 50th / 90th percentile scenarios; not worst/best bounds or individual simulated paths',
                            'assumptions':'Common paths across horizons; Brownian-bridge touch correction (approximate with jumps); regular WTI schedule; uncalibrated.',
                            'sampling_note':'Intervals quantify simulation noise only, not model error or forecast accuracy.'})
    return result
