import copy
import hashlib
import json
from datetime import date,datetime,timedelta,timezone

import pytest

from src import technical_signal as signal
from src import strategic
from src.forecast import OilSnapshot,quote_payload

NOW=datetime(2026,10,26,12,tzinfo=timezone.utc)
NEWS={'coverage':3,'sources':[{}, {}, {}],'vol_multiplier':1.,'jump_intensity':0.,'headlines':[]}


@pytest.fixture(autouse=True)
def frozen_data(tmp_path,monkeypatch):
    monkeypatch.setattr(strategic,'CACHE_PATH',tmp_path/'data.json')
    monkeypatch.setattr(strategic,'CACHE_REPORT',tmp_path/'report.json')
    monkeypatch.setattr(signal,'refresh_if_needed',lambda *args:None)
    signal.cached_setup.cache_clear()


def test_future_and_partial_week_bars_cannot_change_a_setup():
    data=strategic.load_data();asof=date(2026,10,21)
    a=signal.technical_setup(data['bars'],asof);bars=copy.deepcopy(data['bars'])
    for bar in bars:
        if bar['date']>=asof.isoformat():
            for key in ('open','high','low','close'):bar[key]*=10
    assert signal.technical_setup(bars,asof)==a
    assert a['as_of_bar']=='2026-10-20'
    assert a['weekly_as_of']=='2026-10-16'
    assert sum(a['counts'].values())==16
    for direction,levels in a['levels'].items():
        if direction=='long':assert levels['invalidation']<levels['entry']<levels['week_target']<levels['month_target']
        else:assert levels['invalidation']>levels['entry']>levels['week_target']>levels['month_target']>0


def test_new_weekly_bar_is_available_only_after_friday_close():
    bars=strategic.load_data()['bars']
    before=signal.technical_setup(bars,date(2026,10,23))
    after=signal.technical_setup(bars,date(2026,10,24))
    assert before['weekly_as_of']=='2026-10-16'
    assert after['weekly_as_of']=='2026-10-23'


def test_live_signal_is_risk_gated_and_never_invents_a_validated_edge():
    snap=OilSnapshot(101,NOW,102,100)
    data=signal.live_signal(snap,quote_payload(snap,NOW),NEWS,NOW)
    assert data['status']=='stand_aside'
    assert not data['historical_validation']['reliability_gate_passed']
    assert any('historical edge' in r for r in data['blocked_reasons'])
    risky={**NEWS,'vol_multiplier':2,'jump_intensity':50}
    b=signal.live_signal(snap,quote_payload(snap,NOW),risky,NOW)
    assert b['direction']==data['direction']
    assert b['setup']==data['setup']
    assert any('headline risk' in r for r in b['blocked_reasons'])
    stale={**quote_payload(snap,NOW),'usable':False}
    c=signal.live_signal(snap,stale,NEWS,NOW)
    assert any('stale' in r for r in c['blocked_reasons'])


def test_signal_endpoint_ignores_probability_form_inputs_and_deduplicates(tmp_path,monkeypatch):
    import src.forecast as forecast
    import src.intelligence as intelligence
    from fastapi.testclient import TestClient
    from src.main import app
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return NOW
    monkeypatch.setenv('SQLITE_DB_PATH',str(tmp_path/'signals.db'))
    monkeypatch.delenv('DATABASE_URL',raising=False)
    monkeypatch.setattr(forecast,'datetime',Clock)
    monkeypatch.setattr(forecast.feed,'get',lambda:OilSnapshot(101,NOW,102,100))
    monkeypatch.setattr(intelligence,'news_context',lambda now:NEWS)
    with TestClient(app) as client:
        a=client.get('/api/signal')
        b=client.get('/api/signal?target_price=97.5&direction=short&leverage=100')
    assert a.status_code==b.status_code==200
    assert a.headers['cache-control']=='no-store'
    assert a.json()==b.json()
    from src.storage import connect
    with connect() as db:
        assert db.execute('SELECT COUNT(*) FROM technical_signal_log').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM oil_forecasts').fetchone()[0]==0


def test_daily_intrabar_ambiguity_is_resolved_conservatively():
    from scripts.evaluate_signal import trade
    setup={'direction':'long','as_of_bar':'2026-10-23','levels':{'long':{'entry':100,'invalidation':98,'week_target':104,'month_target':106}}}
    bars=[{'date':'2026-10-26','open':99,'high':105,'low':97,'close':103}]*5
    outcome=trade(setup,bars,5)
    assert outcome['outcome']=='stop'
    assert outcome['gross_return']==pytest.approx(-.02)
    assert outcome['ambiguous']
    # A gap past the target is not backfilled into a profitable imaginary entry.
    assert trade(setup,[{**bars[0],'open':105}]+bars[1:],5) is None


def test_rule_report_is_reproducible_and_does_not_claim_profitability():
    from scripts.evaluate_signal import evaluate
    data=strategic.load_data();actual=evaluate(data);saved=json.loads(signal.REPORT_PATH.read_text())
    assert actual==saved
    assert not saved['reliability_gate_passed']
    assert saved['horizons']['1 month']['heldout']['trades']<50
    assert saved['data_sha256']==hashlib.sha256(strategic.DATA_PATH.read_bytes()).hexdigest()


def test_current_session_is_used_only_after_its_new_york_close(tmp_path,monkeypatch):
    data=copy.deepcopy(strategic.load_data())
    data['bars'].append({'date':'2026-10-26','open':103,'high':104,'low':102,'close':103.5})
    path=tmp_path/'with-current-session.json';path.write_text(json.dumps(data))
    monkeypatch.setattr(signal,'data_path',lambda:path)
    morning=signal.live_signal(OilSnapshot(103,NOW,104,102),{'usable':True,'market_open':True},NEWS,NOW)
    after_close=datetime(2026,10,26,22,tzinfo=timezone.utc)
    evening=signal.live_signal(OilSnapshot(103.5,after_close,104,102),{'usable':True,'market_open':True},NEWS,after_close)
    assert morning['as_of_bar']=='2026-10-23'
    assert evening['as_of_bar']=='2026-10-26'
    assert datetime.fromisoformat(evening['valid_until']).astimezone(signal.NY).date()==date(2026,10,27)
