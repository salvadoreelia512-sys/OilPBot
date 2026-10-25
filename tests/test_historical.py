import pytest
from src.historical import load,sync_archive,summary
from src.storage import connect

def test_archive_and_causal_validation_metadata(tmp_path,monkeypatch):
    monkeypatch.delenv('DATABASE_URL',raising=False)
    monkeypatch.setenv('SQLITE_DB_PATH',str(tmp_path/'archive.db'))
    data=sync_archive();sync_archive()
    assert set(data['series'])=={'wti','sp500','gpr'}
    assert data['validation']['test_observations']>=300
    assert data['validation']['continuous_touch_accuracy'] is None
    assert not data['validation']['promoted_direction_model']
    for name,rows in data['series'].items():
        assert rows==sorted(rows,key=lambda r:r['date'])
        assert all((r['value']>=0 if name=='gpr' else r['value']>0) and r['date']>='2024-12-01' for r in rows)
    with connect() as db:
        assert db.execute('SELECT COUNT(*) FROM oil_historical').fetchone()[0]==sum(map(len,data['series'].values()))
        assert db.execute('SELECT COUNT(*) FROM oil_archives').fetchone()[0]==1
