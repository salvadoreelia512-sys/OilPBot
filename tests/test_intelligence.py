from datetime import datetime,timedelta,timezone
import numpy as np
from src.forecast import OilSnapshot,ForecastRequest,estimate
from src.intelligence import enhance

NOW=datetime(2026,10,21,12,tzinfo=timezone.utc)
S=OilSnapshot(75,NOW,76,74)
C={'coverage':0,'sources':[],'drivers':[],'headlines':[],'vol_multiplier':1.,'jump_intensity':0.}

def run(direction='long',leverage=5,context=C):
    q=ForecastRequest(target_price=78 if direction=='long' else 72,direction=direction,leverage=leverage)
    return enhance(estimate(S,q,NOW),S,q,NOW,context=context,observations=[])

def test_monte_carlo_reproducible_nested_and_leverage_independent():
    a=run();b=run(leverage=10)
    assert a['horizons']==b['horizons']
    p=[r['probability'] for r in a['horizons']]
    assert p==sorted(p) and all(0<=v<=1 for v in p)
    assert a['outlook']['action']=='wait'
    assert a['projection']==b['projection']
    for point in a['projection']:assert point['low']<=point['median']<=point['high']

def test_news_changes_risk_and_scenarios_without_invented_direction():
    a=run();b=run(context={**C,'vol_multiplier':2,'jump_intensity':20})
    assert b['model']['annual_volatility']>a['model']['annual_volatility']
    assert b['projection'][-1]['high']-b['projection'][-1]['low']>a['projection'][-1]['high']-a['projection'][-1]['low']
    assert b['outlook']['action']=='wait'

def test_short_probability_and_sampling_error():
    r=run('short')
    assert len(r['horizons'])==6
    assert r['horizons'][-1]['label']=='1 month'
    assert all(0<=h['sampling_error_pp']<=1 for h in r['horizons'])

def test_news_feed_dates_coverage_and_dedup(monkeypatch):
    import src.intelligence as module
    import httpx
    from email.utils import format_datetime
    title='Iran tanker attack threatens Hormuz oil supplies'
    xml=f'<rss><channel><item><title>{title}</title><link>https://example.com/story</link><pubDate>{format_datetime(NOW-timedelta(minutes=10))}</pubDate></item><item><title>Undated oil attack</title></item></channel></rss>'
    monkeypatch.setattr(module,'_until',0)
    monkeypatch.setattr(module,'_cache',None)
    monkeypatch.setattr(module.httpx,'get',lambda url,**kwargs:httpx.Response(200,text=xml,request=httpx.Request('GET',url)))
    data=module.news_context(NOW)
    assert data['coverage']==2
    assert len(data['headlines'])==2
    assert all(h['title']==title for h in data['headlines'])
    assert len(data['drivers'])==1
    assert data['vol_multiplier']>1 or data['jump_intensity']>0
