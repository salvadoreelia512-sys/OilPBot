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
    from src.strategic import SPEC_VERSION, context as strategic_context
    from src.strategic_forecast import calculate as strategic_calculate
    strategy=strategic_context(now)
    if strategy['data_age_days']>10:
        from fastapi import HTTPException
        raise HTTPException(status_code=503,detail='Strategic daily history is out of date; public-data refresh is pending. No current forecast is issued from expired history.')
    sigma=math.sqrt(strategy['short_variance'])
    # No unvalidated intraday momentum or headline direction enters the central estimate.
    drift=0.; direction='wait'
    month_end=now+timedelta(days=30)
    result['horizons'].append({'label':'1 month','hours':720,'trading_hours':round(trading_hours(now,month_end),3),'ends_at_utc':month_end.isoformat()})
    analysis=strategic_calculate(snapshot,request,now,result['horizons'],strategy,context)
    result['projection']=analysis['projection']
    result['strategic']={**strategy,**{k:v for k,v in analysis.items() if k!='projection'},
                         'news_in_central_estimate':False,
                         'interpretation':'Central probabilities use the evaluated daily-volatility baseline. News and fundamental stress are separate unvalidated scenarios; daily-origin validation does not calibrate hourly forecasts or arbitrary live-origin calendar horizons.'}
    can_suggest=False  # No directional candidate has passed an independent validation gate.
    target=snapshot.price*math.exp((1 if direction=='long' else -1)*sigma*math.sqrt(115/(252*23))*.5) if can_suggest else None
    wait_reasons=[]
    wait_reasons.append('No directional model has passed independent validation')
    if context['coverage']<len(SOURCES): wait_reasons.append('news coverage incomplete')
    if not result['quote']['market_open']: wait_reasons.append('market closed')
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
                       'validation':'Weekly/monthly daily-origin TVC target-touch diagnostics are available. Live intraday-origin calibration and headline-based predictive accuracy remain unvalidated.',
                       'unmodeled':'Inventories and US production are dated context and stress inputs. Global demand, futures curve, USD/rates, positioning, AIS, exceptional closures and execution costs are not fully modeled.'}
    result['model'].update({'version':SPEC_VERSION,'annual_volatility':sigma,
                            'basis':'Daily TVC volatility baseline; complex candidates selected before 2019 failed the later upgrade gate; no claimed directional edge',
                            'interval':'Analytical 10th / 50th / 90th percentiles; separate news/fundamental stress scenario, not confidence bounds',
                            'assumptions':'Zero arithmetic drift; shared source price; historical daily volatility; regular WTI schedule. Daily-origin tests do not establish arbitrary live-origin or hourly calibration.',
                            'sampling_note':'Central probabilities are analytical under model assumptions. Stress Monte Carlo noise and historical calibration error do not bound future forecast error.',
                            'directional_edge_validated':False})
    return result
