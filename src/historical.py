"""Versioned proxy history; it is not exact TVC intraday history or a news archive."""
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone

PATH=Path(__file__).parent/'data'/'history.json'

def load():
    return json.loads(PATH.read_text())

def sync_archive():
    from src.storage import connect
    content=PATH.read_bytes();version=hashlib.sha256(content).hexdigest();data=json.loads(content)
    with connect() as db:
        if db.execute('SELECT id FROM oil_archives WHERE id=?',(version,)).fetchone():return data
        rows=[(series,row['date'],row['value'],data['sources'][series]) for series,items in data['series'].items() for row in items]
        db.executemany('INSERT INTO oil_historical(series,day,value,source) VALUES (?,?,?,?) ON CONFLICT(series,day) DO NOTHING',rows)
        db.execute('INSERT INTO oil_archives(id,report) VALUES (?,?) ON CONFLICT(id) DO NOTHING',(version,json.dumps(data['validation'])))
    return data

def summary(data):
    return {'series':{name:{'count':len(rows),'first':rows[0]['date'],'last':rows[-1]['date'],'source':data['sources'][name]} for name,rows in data['series'].items()},
            'validation':data['validation'],'volatility_prior':data['annual_volatility_prior'],
            'limitations':'WTI spot and S&P 500 are proxies, not exact TVC intraday data. GPR is a newspaper-based risk index, not all historical headlines. Revised data; archive does not update automatically.'}
