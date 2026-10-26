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
    assert data['coverage']==len(module.SOURCES)
    assert len(data['headlines'])==1
    assert all(h['title']==title for h in data['headlines'])
    assert len(data['drivers'])==1
    assert data['vol_multiplier']>1 or data['jump_intensity']>0

def test_small_risk_change_does_not_resample_diffusion():
    a=run();b=run(context={**C,'vol_multiplier':1.001})
    assert max(abs(x['probability']-y['probability']) for x,y in zip(a['horizons'],b['horizons'])) < .02

def test_volatility_has_no_warmup_threshold_jump():
    q=ForecastRequest(target_price=78,direction='long')
    def with_count(count):
        observations=[((NOW-timedelta(hours=4)+timedelta(hours=4*i/(count-1))).timestamp(),75) for i in range(count)]
        return enhance(estimate(S,q,NOW),S,q,NOW,context=C,observations=observations)
    before,after=with_count(20),with_count(21)
    assert abs(before['model']['annual_volatility']-after['model']['annual_volatility'])<.01
    assert max(abs(a['probability']-b['probability']) for a,b in zip(before['horizons'],after['horizons']))<.02


def test_news_excludes_articles_older_than_24_hours(monkeypatch):
    import httpx
    import src.intelligence as module
    from email.utils import format_datetime
    old = format_datetime(NOW-timedelta(days=4))
    expired = format_datetime(NOW-timedelta(days=8))
    xml=f'<rss><channel><item><title>Refineries face severe tanker attack</title><link>https://example.com/old</link><pubDate>{old}</pubDate></item><item><title>Expired oil news</title><pubDate>{expired}</pubDate></item></channel></rss>'
    monkeypatch.setattr(module,'_until',0)
    monkeypatch.setattr(module,'_cache',None)
    monkeypatch.setattr(module.httpx,'get',lambda url,**kwargs:httpx.Response(200,text=xml,request=httpx.Request('GET',url)))
    data=module.news_context(NOW)
    assert data['coverage']==len(module.SOURCES)
    assert data['headlines']==[]
    assert data['drivers']==[]
    assert data['vol_multiplier']==1
    assert data['checked_at_utc']


def test_failed_news_retries_quickly_and_recovers_independently_of_quotes(monkeypatch):
    import httpx
    import src.intelligence as module
    import src.forecast as forecast
    from fastapi.testclient import TestClient
    from src.main import app
    clock=[1000.]
    broken=[True]
    def fetch(url,**kwargs):
        # An HTML error page with HTTP 200 must not count as a healthy RSS feed.
        xml='<html>Blocked</html>' if broken[0] else '<rss><channel></channel></rss>'
        return httpx.Response(200,text=xml,request=httpx.Request('GET',url))
    def no_quote():
        raise AssertionError('News must not depend on a market quote')
    monkeypatch.setattr(module,'_until',0)
    monkeypatch.setattr(module,'_cache',None)
    monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(module.httpx,'get',fetch)
    monkeypatch.setattr(forecast.feed,'get',no_quote)
    data=module.news_context(NOW)
    assert data['coverage']==0
    broken[0]=False
    assert module.news_context(NOW)['coverage']==0
    clock[0]+=15
    assert module.news_context(NOW)['coverage']==len(module.SOURCES)
    with TestClient(app) as client:
        response=client.get('/api/news')
        assert response.status_code==200
        assert response.headers['cache-control']=='no-store'
        assert response.json()['coverage']==len(module.SOURCES)


def test_neutral_monte_carlo_matches_exact_barrier_probabilities():
    # Checks numerical correctness under known GBM assumptions, not market accuracy.
    import math
    from src.simulation import closed_form_barrier_touch_prob
    for direction in ['long','short']:
        forecast=run(direction)
        sigma=forecast['model']['annual_volatility']
        for horizon in forecast['horizons']:
            expected=closed_form_barrier_touch_prob(S.price,forecast['target_price'],sigma,horizon['trading_hours']/(252*23))
            error=4*math.sqrt(expected*(1-expected)/12000)+.002
            assert abs(horizon['probability']-expected)<=error


def test_news_ranks_real_headlines_and_strictly_enforces_date_window(monkeypatch):
    import httpx
    import src.intelligence as module
    from email.utils import format_datetime
    def article(title, age):
        return f'<item><title>{title}</title><link>https://example.com/{age}</link><source>Reuters</source><pubDate>{format_datetime(NOW-timedelta(seconds=age))}</pubDate></item>'
    xml='<rss><channel>'+''.join([
        article('Oil tanker blockade threatens Hormuz supplies',3600),
        article('Oil prices rise in quiet trading',60),
        article('Oil old story',86401),article('Oil future story',-60),
        '<item><title>Oil undated</title><link>https://example.com/undated</link></item>',
    ])+'</channel></rss>'
    monkeypatch.setattr(module,'_until',0)
    monkeypatch.setattr(module,'_cache',None)
    monkeypatch.setattr(module.httpx,'get',lambda url,**kw:httpx.Response(200,text=xml,request=httpx.Request('GET',url)))
    data=module.news_context(NOW)
    assert len(data['headlines'])==2  # Identical stories across feeds appear once.
    assert 'blockade' in data['headlines'][0]['title']
    assert all(h['source']=='Reuters' for h in data['headlines'])
    assert all(h['used_for_risk'] for h in data['headlines'])
    # Expire cached stories even before another successful feed fetch.
    assert module.news_context(NOW+timedelta(hours=25))['headlines']==[]


def test_conditional_setup_has_a_direction_entry_and_long_term_targets():
    for direction in ['long','short']:
        result=run(direction)
        setup=result['outlook']['setup']
        assert setup['direction']==direction
        assert setup['status']=='conditional scenario'
        assert result['outlook']['action']=='wait'  # No forced directional evidence.
        if direction=='long':
            assert S.price<setup['entry_price']<setup['week_target']<setup['month_target']
        else:
            assert S.price>setup['entry_price']>setup['week_target']>setup['month_target']>0
        assert 'selected direction' in setup['basis']
