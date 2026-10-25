from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src import main
from src.config import BotSettings
from src.prices import BarRepository, PriceService, PriceAdapter, PriceTick, StaleDataError, AllSourcesDownError
from src.simulation import SimulationEngine
from src.volatility import VolatilityEngine


class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc).astimezone(tz)


class Feed(PriceAdapter):
    name = 'fixture'

    def fetch_latest_tick(self, symbol):
        return PriceTick(symbol, 75.0, FixedClock.now(timezone.utc), self.name)


@pytest.fixture
def client(monkeypatch, tmp_path):
    repo = BarRepository(str(tmp_path / 'bars.db'))
    settings = BotSettings(sqlite_db_path=repo.db_path)
    monkeypatch.setattr(main, 'datetime', FixedClock)
    monkeypatch.setattr(main, 'bar_repo', repo)
    monkeypatch.setattr(main, 'price_service', PriceService([Feed()], settings, repo))
    monkeypatch.setattr(main, 'sim_engine', SimulationEngine(n_paths=2000, seed=42))
    with TestClient(main.app) as client:
        yield client


@pytest.mark.parametrize('bars', [0, 4, 5, 6, 65])
def test_prediction_with_sparse_and_real_history(client, bars):
    now = FixedClock.now(timezone.utc)
    for n in range(bars):
        main.bar_repo.record_tick_as_bar(PriceTick('CL=F', 75 + n * .01, now - timedelta(minutes=n + 1), 'fixture'))
    response = client.post('/api/predict', json={'target_price':78.5, 'direction':'long'})
    assert response.status_code == 200, response.text
    data = response.json()
    assert 0 <= data['simulation']['prob_touch_target'] <= 1
    assert data['spot']['source'] == 'fixture'
    assert data['history']['warmup'] == (bars + 1 < 6)
    assert data['simulation']['high_bands']['p50'] >= data['spot']['price']
    assert data['simulation']['low_bands']['p50'] <= data['spot']['price']


@pytest.mark.parametrize('error,status', [(StaleDataError('REFUSED: stale'),503),(AllSourcesDownError('down'),502)])
def test_feed_failure_and_recovery(client, monkeypatch, error, status):
    service = main.price_service
    class Failed:
        def get_fresh_price(self, **kwargs):
            raise error
    monkeypatch.setattr(main, 'price_service', Failed())
    for route in ['/api/predict', '/predict']:
        response = client.post(route,json={'target_price':78,'direction':'long'})
        assert response.status_code == status
        assert isinstance(response.json()['detail'], str)
    assert client.get('/api/quote').status_code == status
    monkeypatch.setattr(main, 'price_service', service)
    assert client.post('/api/predict',json={'target_price':78,'direction':'long'}).status_code == 200
    assert client.get('/api/quote').json()['spot']['price'] == 75


@pytest.mark.parametrize('payload', [
    {'target_price':-1,'direction':'long'},
    {'target_price':78,'direction':'other'},
    {'target_price':78,'direction':'short','leverage':1,'required_return':1},
    {'target_price':'Infinity','direction':'long'},
])
def test_invalid_requests(client,payload):
    assert client.post('/api/predict',json=payload).status_code == 422


def test_today_cutoff_does_not_roll_to_tomorrow(client,monkeypatch):
    engine = VolatilityEngine()
    assert engine.remaining_session_years(datetime(2026,10,21,19,30,tzinfo=timezone.utc)) == (0,0)
    assert engine.remaining_session_years(datetime(2026,10,21,19,45,tzinfo=timezone.utc)) == (0,0)
    monkeypatch.setattr(main.vol_engine,'remaining_session_years',lambda **kwargs:(0,0))
    assert client.post('/api/predict',json={'target_price':78,'direction':'long'}).status_code == 409


def test_ui_from_other_working_directory(client, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert 'Intraday Merton' in client.get('/').text
    assert client.get('/api/health').json()['status'] == 'stale_or_down'
    assert client.get('/api/quote').status_code == 200
    assert client.get('/api/health').json()['status'] == 'ok'


def test_failover_keeps_same_instrument(tmp_path):
    calls = []
    class NamedFeed(Feed):
        def __init__(self,name):
            self.name = name
        def fetch_latest_tick(self,symbol):
            calls.append((self.name,symbol))
            if self.name == 'finnhub':
                raise ConnectionError('private token must not be logged')
            return super().fetch_latest_tick(symbol)
    for instrument, expected in [('CL=F',[('yahoo','CL=F')]),('USO',[('finnhub','USO'),('yahoo','USO')])]:
        calls.clear()
        service=PriceService([NamedFeed('finnhub'),NamedFeed('yahoo')],BotSettings(market_symbol=instrument,sqlite_db_path=str(tmp_path/'symbols.db')))
        assert service.get_fresh_price(now_utc=FixedClock.now(timezone.utc)).symbol == instrument
        assert calls == expected
        assert 'private token' not in str(service.health_report())


@pytest.mark.parametrize('price,timestamp', [
    (float('nan'), FixedClock.now(timezone.utc)),
    (-1, FixedClock.now(timezone.utc)),
    (75, FixedClock.now(timezone.utc)+timedelta(hours=1)),
    (75, datetime(2026,10,21,12)),
])
def test_invalid_provider_data_refused(tmp_path,price,timestamp):
    class InvalidFeed(Feed):
        def fetch_latest_tick(self,symbol):
            return PriceTick(symbol,price,timestamp,self.name)
    service=PriceService([InvalidFeed()],BotSettings(sqlite_db_path=str(tmp_path/'invalid.db')))
    with pytest.raises(AllSourcesDownError):
        service.get_fresh_price(now_utc=FixedClock.now(timezone.utc))
