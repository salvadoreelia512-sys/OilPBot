import copy
import hashlib
import json
from datetime import datetime,timedelta,timezone

import pytest

from src import strategic


@pytest.fixture(autouse=True)
def bundled_archive_fixture(tmp_path, monkeypatch, request):
    # Local runtime refreshes must not change the frozen validation test fixture.
    monkeypatch.setattr(strategic, 'CACHE_PATH', tmp_path/'runtime.json')
    monkeypatch.setattr(strategic, 'CACHE_REPORT', tmp_path/'runtime-report.json')
    if request.node.name != 'test_refresh_is_atomic_and_cannot_promote_an_unreviewed_model':
        monkeypatch.setattr(strategic, 'refresh_if_needed', lambda *args: None)


def test_sources_and_validation_fingerprint_are_consistent():
    data=strategic.load_data();report=strategic.load_validation()
    assert len(data['bars'])>=4000
    assert data['instrument']=='TVC:USOIL'
    assert data['session_identity']['pro_name']=='TVC:USOIL'
    assert data['session_identity']['session']=='1800-1700'
    assert data['bars'][-1]['date']<data['built_at_utc'][:10]
    assert report['daily_tvc_touch_evaluated'] is True
    assert all(data['bars'][i]['date']<data['bars'][i+1]['date'] for i in range(len(data['bars'])-1))
    assert report['data_sha256']==hashlib.sha256(strategic.DATA_PATH.read_bytes()).hexdigest()
    assert report['exact_tvc_touch_validated'] is False
    assert report['directional_edge_validated'] is False
    assert report['horizons']['1 week']['models']['baseline']['origins']>=300
    assert report['horizons']['1 month']['models']['baseline']['origins']>=80
    assert not report['promoted']  # Current candidates did not clear the predeclared gate.
    assert report['selected_model']=='baseline'


def test_future_prices_and_unreleased_fundamentals_cannot_change_features():
    data=strategic.load_data();asof='2024-01-02'
    a=strategic.parameters(data['bars'],data['weekly'],asof)
    changed=copy.deepcopy(data)
    for bar in changed['bars']:
        if bar['date']>asof:
            for key in ('open','high','low','close'):bar[key]*=10
    for rows in changed['weekly'].values():
        for row in rows:
            if row['date']>'2023-12-26':row['value']*=10
    assert a==strategic.parameters(changed['bars'],changed['weekly'],asof)


def test_seven_day_weekly_publication_lag_is_enforced():
    data=strategic.load_data()
    before=strategic.parameters(data['bars'],data['weekly'],'2026-10-22')
    after=strategic.parameters(data['bars'],data['weekly'],'2026-10-23')
    assert before['fundamentals']['inventories']['week_ending']=='2026-10-09'
    assert after['fundamentals']['inventories']['week_ending']=='2026-10-16'
    assert after['fundamentals']['inventories']['available_after']=='2026-10-23'


def test_bad_or_mismatched_report_never_promotes_a_candidate(monkeypatch):
    monkeypatch.setattr(strategic,'load_validation',lambda:{'data_sha256':'bad','promoted':True,'selected_model':'regime_fundamentals'})
    monkeypatch.setattr(strategic,'refresh_if_needed',lambda *args:None)
    context=strategic.context(datetime(2026,10,26,tzinfo=timezone.utc))
    assert not context['proxy_validation_promoted']
    assert context['model']=='baseline'


def test_integrated_regime_variance_is_additive_and_positive():
    state={'short_variance':.6**2,'long_variance':.3**2,'fundamental_scale':1.1}
    early=strategic.integrated_variance(state,1/252,'regime')
    late=strategic.integrated_variance(state,21/252,'regime')
    assert 0<early<late
    assert late/(21/252)<early/(1/252)
    assert strategic.integrated_variance(state,21/252,'regime_fundamentals')==pytest.approx(late*1.1**2)


def test_heldout_report_can_be_reproduced_without_tuning():
    from scripts.evaluate_strategic import evaluate
    actual=evaluate(strategic.load_data());saved=strategic.load_validation()
    assert actual==saved


def test_strategy_endpoint_exposes_sources_and_limits():
    from fastapi.testclient import TestClient
    from src.main import app
    with TestClient(app) as client:
        response=client.get('/api/strategy')
    assert response.status_code==200
    assert response.headers['cache-control']=='no-store'
    assert response.json()['coverage_gaps']
    assert response.json()['sources']['inventories'].startswith('https://www.eia.gov/')


def test_refresh_is_atomic_and_cannot_promote_an_unreviewed_model(tmp_path,monkeypatch):
    import scripts.build_strategic_data as builder
    import scripts.evaluate_strategic as evaluator
    data=strategic.load_data()
    monkeypatch.setattr(strategic,'CACHE_PATH',tmp_path/'current.json')
    monkeypatch.setattr(strategic,'CACHE_REPORT',tmp_path/'report.json')
    monkeypatch.setattr(strategic,'_refresh_attempt',0.)
    def build(refresh,destination):
        assert refresh
        destination.write_text(json.dumps(data))
        return data
    def evaluate(payload,fingerprint):
        return {'data_sha256':fingerprint,'promoted':True,'selected_model':'regime_fundamentals'}
    monkeypatch.setattr(builder,'build',build)
    monkeypatch.setattr(evaluator,'evaluate',evaluate)
    strategic.refresh_if_needed(datetime(2026,10,26,tzinfo=timezone.utc),'2026-10-19')
    assert strategic._refresh_lock.acquire(timeout=5)
    strategic._refresh_lock.release()
    report=json.loads(strategic.CACHE_REPORT.read_text())
    assert report['data_sha256']==hashlib.sha256(strategic.CACHE_PATH.read_bytes()).hexdigest()
    assert not report['promoted'] and report['selected_model']=='baseline'
    assert not list(tmp_path.glob('*.tmp'))


def test_expired_daily_history_blocks_a_new_forecast(monkeypatch):
    import src.strategic as module
    from src.intelligence import enhance
    from src.forecast import OilSnapshot,ForecastRequest,estimate
    from fastapi import HTTPException
    now=datetime(2026,10,26,12,tzinfo=timezone.utc)
    snapshot=OilSnapshot(100,now,101,99);request=ForecastRequest(target_price=105,direction='long')
    monkeypatch.setattr(module,'context',lambda now:{'data_age_days':11})
    with pytest.raises(HTTPException) as error:
        enhance(estimate(snapshot,request,now),snapshot,request,now,
                context={'coverage':0,'vol_multiplier':1,'jump_intensity':0},observations=[])
    assert error.value.status_code==503
    assert 'out of date' in error.value.detail


def test_stale_or_corrupt_cache_cannot_hide_newer_bundled_history(tmp_path,monkeypatch):
    data=copy.deepcopy(strategic.load_data());data['bars']=data['bars'][:-30]
    cached=tmp_path/'cached.json';report=tmp_path/'report.json'
    cached.write_text(json.dumps(data));report.write_text('{}')
    monkeypatch.setattr(strategic,'CACHE_PATH',cached)
    monkeypatch.setattr(strategic,'CACHE_REPORT',report)
    assert strategic.data_path()==strategic.DATA_PATH
    cached.write_text('not valid json')
    assert strategic.data_path()==strategic.DATA_PATH
