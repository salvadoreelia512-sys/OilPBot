from datetime import datetime,timedelta,timezone
import os
import pytest
from src.forecast import OilSnapshot
from src import storage

@pytest.fixture
def database(tmp_path,monkeypatch):
    # STORAGE_TEST_URL may target a disposable local PostgreSQL database only.
    if os.getenv('STORAGE_TEST_URL'):
        monkeypatch.setenv('DATABASE_URL',os.environ['STORAGE_TEST_URL'])
    else:
        monkeypatch.delenv('DATABASE_URL',raising=False)
        monkeypatch.setenv('SQLITE_DB_PATH',str(tmp_path/'storage.db'))
    with storage.connect() as db:
        for table in ['oil_outcomes','oil_forecasts','oil_news','tvc_observations']: db.execute('DELETE FROM '+table)


def result(now):
    return {'quote':{'price':75,'timestamp_utc':now.isoformat()},'target_price':78,'direction':'long',
            'model':{'annual_volatility':.3},'outlook':{'action':'wait'},
            'evidence':{'observations':1,'news':{'coverage':0,'vol_multiplier':1,'jump_intensity':0,'headlines':[]}},
            'horizons':[{'hours':1,'ends_at_utc':(now+timedelta(hours=1)).isoformat(),'probability':.2}]}


def test_persistence_and_idempotence(database):
    now=datetime(2026,10,21,12,tzinfo=timezone.utc)
    q=OilSnapshot(75,now,76,74)
    assert len(storage.record_quote(q))==1
    assert len(storage.record_quote(q))==1
    a=storage.save_forecast(result(now),now)
    assert a==storage.save_forecast(result(now),now)
    stats=storage.statistics(now)
    assert stats['observations']==1 and stats['forecasts']==1 and stats['outcomes']['pending']==1


def test_touch_and_missing_coverage_are_distinct(database):
    now=datetime(2026,10,21,12,tzinfo=timezone.utc)
    storage.save_forecast(result(now),now)
    stats=storage.statistics(now+timedelta(hours=2))
    assert stats['outcomes']['insufficient_coverage']==1
    assert stats['observation_based_brier_by_horizon']==[]
    other=now+timedelta(hours=3)
    storage.save_forecast(result(other),other)
    storage.record_quote(OilSnapshot(79,other+timedelta(minutes=10),80,74))
    stats=storage.statistics(other+timedelta(hours=2))
    assert stats['outcomes']['observed_touch']==1


def test_configured_database_never_silently_falls_back(monkeypatch):
    monkeypatch.setenv('DATABASE_URL','postgresql://127.0.0.1:1/invalid?connect_timeout=1')
    with pytest.raises(storage.StorageUnavailable):
        with storage.connect():pass

def test_statistics_failure_is_safe_json(monkeypatch):
    from fastapi.testclient import TestClient
    from src.main import app
    monkeypatch.setenv('DATABASE_URL','postgresql://127.0.0.1:1/invalid?connect_timeout=1')
    with TestClient(app) as client:
        response=client.get('/api/statistics')
    assert response.status_code==503
    assert 'postgresql://' not in response.text
    assert 'DATABASE_URL' in response.json()['detail']
