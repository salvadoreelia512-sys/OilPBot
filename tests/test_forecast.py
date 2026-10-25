import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import src.forecast as forecast
from src.main import app

NOW = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
SNAP = forecast.OilSnapshot(75, NOW, 76, 74)


def page(symbol='TVC:USOIL', **bar):
    body={'wrapper':{'data':{'symbol':{'pro_symbol':symbol,'daily_bar':{'close':'75','high':'76','low':'74','data_update_time':str(NOW.timestamp()),**bar}}}}}
    return '<script type="application/prs.init-data+json">'+json.dumps(body)+'</script>'


def test_parser_requires_exact_instrument_and_matching_bar():
    assert forecast.parse_snapshot(page()) == SNAP
    for html in [page(symbol='NYMEX:CL1!'),page(close='nan'),page(close='99'),'<html>Unavailable</html>']:
        with pytest.raises(ValueError): forecast.parse_snapshot(html)


@pytest.mark.parametrize('direction,target',[('long',78),('short',72)])
def test_all_horizons_monotonic_and_leverage_independent(direction,target):
    first=forecast.estimate(SNAP,forecast.ForecastRequest(target_price=target,direction=direction,leverage=1),NOW)
    second=forecast.estimate(SNAP,forecast.ForecastRequest(target_price=target,direction=direction,leverage=10),NOW)
    p=[r['probability'] for r in first['horizons']]
    assert len(p)==5 and p==sorted(p) and all(0<=v<=1 for v in p)
    assert first['horizons']==second['horizons']
    assert second['leveraged_return_percent']==pytest.approx(first['leveraged_return_percent']*10)
    assert len(first['projection'])==169
    for row in first['projection']:
        assert row['low']<=row['median']<=row['high']


def test_model_time_and_direction_validation():
    for direction,target in [('long',74),('short',76),('long',75)]:
        with pytest.raises(HTTPException) as error:
            forecast.estimate(SNAP,forecast.ForecastRequest(target_price=target,direction=direction),NOW)
        assert error.value.status_code==422
    with pytest.raises(HTTPException):
        forecast.estimate(replace(SNAP,timestamp=NOW-timedelta(hours=1)),forecast.ForecastRequest(target_price=78,direction='long'),NOW)


def test_weekend_uses_last_close_and_respects_closed_hours():
    now=datetime(2026,10,25,16,tzinfo=timezone.utc)
    last_close=datetime(2026,10,23,20,59,59,tzinfo=timezone.utc)
    snapshot=replace(SNAP,timestamp=last_close)
    result=forecast.estimate(snapshot,forecast.ForecastRequest(target_price=78,direction='long'),now)
    assert result['quote']['status']=='market_closed'
    assert result['horizons'][0]['trading_hours']==0
    assert result['horizons'][0]['probability']==0
    assert result['horizons'][-1]['probability']>0
    assert forecast.trading_hours(now,now+timedelta(days=7))==115
    # Stale Friday morning quotes are not treated as the Friday close.
    assert not forecast.quote_payload(replace(SNAP,timestamp=last_close-timedelta(hours=4)),now)['usable']
    # The same old quote becomes unusable after reopening.
    assert not forecast.quote_payload(snapshot,datetime(2026,10,25,23,tzinfo=timezone.utc))['usable']


def test_daily_break_and_dst_are_accounted_for():
    assert not forecast.market_open(datetime(2026,10,21,21,30,tzinfo=timezone.utc))
    assert forecast.market_open(datetime(2026,10,21,22,0,tzinfo=timezone.utc))
    # Following DST change, Sunday opens at 23:00 UTC rather than 22:00.
    assert not forecast.market_open(datetime(2026,11,1,22,30,tzinfo=timezone.utc))
    assert forecast.market_open(datetime(2026,11,1,23,0,tzinfo=timezone.utc))


def test_feed_caching_backoff_and_recovery(monkeypatch):
    calls=[];clock=[1000.];fail=[False]
    def get(*args,**kwargs):
        calls.append(1)
        if fail[0]: raise httpx.ConnectError('private detail')
        return httpx.Response(200,text=page(),request=httpx.Request('GET',forecast.SOURCE_URL))
    monkeypatch.setattr(forecast.httpx,'get',get)
    monkeypatch.setattr(forecast.time,'monotonic',lambda:clock[0])
    feed=forecast.OilFeed()
    assert feed.get()==SNAP
    assert feed.get()==SNAP and len(calls)==1
    clock[0]+=61;fail[0]=True
    assert feed.get()==SNAP and len(calls)==2
    assert feed.last_error=='Oil source unavailable or quote format changed'
    feed.get();assert len(calls)==2
    clock[0]+=31;fail[0]=False
    assert feed.get()==SNAP and len(calls)==3 and feed.last_error is None


def test_no_feed_gives_explicit_503(monkeypatch):
    def fail(*args,**kwargs): raise httpx.ConnectError('not public')
    monkeypatch.setattr(forecast.httpx,'get',fail)
    with pytest.raises(HTTPException) as error: forecast.OilFeed().get()
    assert error.value.status_code==503
    assert 'not public' not in error.value.detail


def test_endpoints_and_minimal_inputs(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return NOW
    monkeypatch.setattr(forecast,'datetime',Clock)
    monkeypatch.setattr(forecast.feed,'get',lambda:SNAP)
    with TestClient(app) as client:
        assert client.get('/api/market').json()['quote']['symbol']=='TVC:USOIL'
        r=client.post('/api/forecast',json={'target_price':78,'direction':'long','leverage':5})
        assert r.status_code==200,r.text
        assert len(r.json()['horizons'])==6
        for payload in [{'target_price':-1,'direction':'long'}, {'target_price':78,'direction':'long','ovx':32}, {'target_price':78,'direction':'long','leverage':0}]:
            assert client.post('/api/forecast',json=payload).status_code==422
        assert 'Price data mode' not in client.get('/').text
        assert client.get('/app.js').status_code==200
