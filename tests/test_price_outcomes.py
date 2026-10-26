import hashlib,json
from datetime import datetime,timezone
from pathlib import Path

import numpy as np
import pytest

from src.price_outcomes import BAND,KEYS,REPORT_PATH,baseline_probabilities,percentage_units,summarize


def test_disjoint_terminal_categories_and_display_total():
    samples=np.array([[100]*6,[99,99.5,100,100.1,101,102]],dtype=float)
    result=summarize(samples,1,100)
    assert [g['sample_count'] for g in result['groups']]==[2,2,2]
    assert sum(g['percentage_units'] for g in result['groups'])==1000
    assert sum(g['probability'] for g in result['groups'])==pytest.approx(1)
    assert result['near_tie']
    assert result['percentage_total']==100
    assert all(len(g['curve'])==2 for g in result['groups'])
    for p in ([1/3]*3,[.000001,.499999,.5],[0,1,0]):assert sum(percentage_units(p))==1000


def test_closed_horizon_and_empty_groups_have_no_fake_paths():
    result=summarize(np.full((2,20),100.),1,100)
    assert [g['percentage_units'] for g in result['groups']]==[0,1000,0]
    assert result['most_likely']==['middle']
    assert result['groups'][0]['curve']==[] and result['groups'][0]['endpoint'] is None
    assert result['groups'][1]['curve']==[100,100]
    assert np.array_equal(baseline_probabilities(0),[0,1,0])
    with pytest.raises(ValueError):summarize(np.array([[100],[float('nan')]]),1,100)


def test_new_report_matches_terminal_task_and_can_be_reproduced():
    from src.strategic import DATA_PATH
    from scripts.evaluate_price_outcomes import evaluate
    raw=DATA_PATH.read_bytes();saved=json.loads(REPORT_PATH.read_text());actual=evaluate(json.loads(raw),hashlib.sha256(raw).hexdigest())
    assert actual==saved
    assert saved['horizons']['1 week']['origins']>=300
    assert saved['horizons']['1 month']['origins']>=80
    assert all('multiclass_brier' in r for r in saved['horizons'].values())


def test_outlook_api_no_target_required_and_does_not_create_target_forecasts(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    import src.forecast as forecast
    import src.intelligence as intelligence
    from src.main import app
    from src.storage import connect
    now=datetime(2026,10,26,12,tzinfo=timezone.utc)
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return now
    monkeypatch.setenv('SQLITE_DB_PATH',str(tmp_path/'outlook.db'));monkeypatch.delenv('DATABASE_URL',raising=False)
    monkeypatch.setattr(forecast,'datetime',Clock)
    monkeypatch.setattr(forecast.feed,'get',lambda:forecast.OilSnapshot(100,now,101,99))
    monkeypatch.setattr(intelligence,'news_context',lambda now:{'vol_multiplier':1.2,'jump_intensity':10,'coverage':3,'sources':[],'headlines':[]})
    with TestClient(app) as c:
        a=c.post('/api/outlook',json={});b=c.post('/api/outlook',json={'reference_price':97.5})
        assert a.status_code==b.status_code==200
        left,right=a.json(),b.json()
        assert left['outcomes']==right['outcomes']  # Chart reference cannot change likelihoods.
        assert left['reference_price'] is None and right['reference_price']==97.5
        assert set(left['outcomes'])=={'1','3','5','24','168','720'}
        for result in left['outcomes'].values():
            assert sum(g['percentage_units'] for g in result['groups'])==1000
            assert sum(g['sample_count'] for g in result['groups'])==6000
        for body in [{'reference_price':-1},{'reference_price':float('inf')},{'direction':'long'}]:
            if body.get('reference_price')==float('inf'):continue  # HTTP JSON cannot encode infinity.
            assert c.post('/api/outlook',json=body).status_code==422
    with connect() as db:
        assert db.execute('SELECT COUNT(*) FROM price_outlook_log').fetchone()[0]==2
        assert db.execute('SELECT COUNT(*) FROM oil_forecasts').fetchone()[0]==0
        evidence=json.loads(db.execute('SELECT evidence FROM price_outlook_log LIMIT 1').fetchone()[0])
        assert 'curve' not in evidence['outcomes']['720']['groups'][0]
