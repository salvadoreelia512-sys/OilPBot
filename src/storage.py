"""Portable persistent evidence store. DATABASE_URL selects PostgreSQL, otherwise SQLite.
Never fall back silently when a configured PostgreSQL connection fails.
"""
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from src.config import BotSettings

class StorageUnavailable(RuntimeError):
    pass


MODEL_VERSION = 'news-mc-v2-history'


class Connection:
    def __init__(self, raw, postgres):
        self.raw, self.postgres = raw, postgres
    def executemany(self, sql, rows):
        return self.raw.cursor().executemany(sql.replace('?', '%s') if self.postgres else sql, rows)
    def execute(self, sql, values=()):
        return self.raw.execute(sql.replace('?', '%s') if self.postgres else sql, values)


@contextmanager
def connect():
    url = os.getenv('DATABASE_URL', '')
    try:
        if url:
            import psycopg
            raw = psycopg.connect(url, connect_timeout=10)
        else:
            raw = sqlite3.connect(BotSettings().sqlite_db_path, timeout=10)
    except Exception:
        raise StorageUnavailable('Persistent database connection unavailable') from None
    try:
        db = Connection(raw, bool(url))
        for statement in SCHEMA:
            db.execute(statement)
        yield db
        raw.commit()
    except Exception:
        raw.rollback()
        raise StorageUnavailable("Persistent database operation unavailable") from None
    finally:
        raw.close()


SCHEMA = [
    'CREATE TABLE IF NOT EXISTS oil_historical (series TEXT NOT NULL, day TEXT NOT NULL, value DOUBLE PRECISION NOT NULL, source TEXT NOT NULL, PRIMARY KEY(series,day))',
    'CREATE TABLE IF NOT EXISTS oil_archives (id TEXT PRIMARY KEY, report TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS tvc_observations (ts DOUBLE PRECISION PRIMARY KEY, price DOUBLE PRECISION NOT NULL)',
    'CREATE TABLE IF NOT EXISTS oil_news (id TEXT PRIMARY KEY, published DOUBLE PRECISION NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS oil_forecasts (id TEXT PRIMARY KEY, created DOUBLE PRECISION NOT NULL, quote_ts DOUBLE PRECISION NOT NULL, target DOUBLE PRECISION NOT NULL, direction TEXT NOT NULL, model_version TEXT NOT NULL, evidence TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS oil_outcomes (forecast_id TEXT NOT NULL, hours INTEGER NOT NULL, due DOUBLE PRECISION NOT NULL, probability DOUBLE PRECISION NOT NULL, outcome INTEGER, status TEXT NOT NULL, PRIMARY KEY(forecast_id,hours))',
    'CREATE INDEX IF NOT EXISTS oil_outcomes_due ON oil_outcomes(status,due)',
]


def record_quote(snapshot):
    with connect() as db:
        db.execute('INSERT INTO tvc_observations(ts,price) VALUES (?,?) ON CONFLICT(ts) DO NOTHING', (snapshot.timestamp.timestamp(), snapshot.price))
        rows = db.execute('SELECT ts,price FROM tvc_observations WHERE ts<=? ORDER BY ts DESC LIMIT 1000', (snapshot.timestamp.timestamp(),)).fetchall()
    return rows[::-1]


def save_forecast(result, now):
    context = result['evidence']['news']
    compact = {'model':result['model'], 'outlook':result['outlook'],
               'observations':result['evidence']['observations'],
               'news_coverage':context['coverage'], 'news_vol_multiplier':context['vol_multiplier'],
               'news_jump_intensity':context['jump_intensity'],
               'news_ids':[], 'quote_price':result['quote']['price']}
    created = now.timestamp()
    quote_ts = datetime.fromisoformat(result['quote']['timestamp_utc']).timestamp()
    key = json.dumps([int(created//60),quote_ts,result['target_price'],result['direction'],
                      result['model']['annual_volatility'],[r['probability'] for r in result['horizons']], MODEL_VERSION])
    fid = hashlib.sha256(key.encode()).hexdigest()
    with connect() as db:
        for item in context['headlines']:
            published = datetime.fromisoformat(item['published_utc']).timestamp()
            nid = hashlib.sha256(json.dumps([item['url'],item['title'],published]).encode()).hexdigest()
            compact['news_ids'].append(nid)
            db.execute('INSERT INTO oil_news VALUES (?,?,?,?) ON CONFLICT(id) DO NOTHING', (nid,published,item['title'],item['url']))
        db.execute('INSERT INTO oil_forecasts VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                   (fid,created,quote_ts,result['target_price'],result['direction'],MODEL_VERSION,json.dumps(compact)))
        for row in result['horizons']:
            db.execute('INSERT INTO oil_outcomes VALUES (?,?,?,?,?,?) ON CONFLICT(forecast_id,hours) DO NOTHING',
                       (fid,row['hours'],datetime.fromisoformat(row['ends_at_utc']).timestamp(),row['probability'],None,'pending'))
    return fid


def evaluate(now):
    """Only confirmed touches or sufficiently covered misses are labeled.
    Missing observations do not establish that a target was missed.
    """
    from src.forecast import trading_hours
    with connect() as db:
        pending = db.execute('SELECT o.forecast_id,o.hours,o.due,f.created,f.target,f.direction FROM oil_outcomes o JOIN oil_forecasts f ON f.id=o.forecast_id WHERE o.status=? AND o.due<=? ORDER BY o.due LIMIT 100', ('pending',now.timestamp())).fetchall()
        for fid,hours,due,start,target,direction in pending:
            rows = db.execute('SELECT ts,price FROM tvc_observations WHERE ts>=? AND ts<=? ORDER BY ts', (start,due)).fetchall()
            touched = any(price>=target if direction=='long' else price<=target for _,price in rows)
            timestamps=[start]+[ts for ts,_ in rows]+[due]
            covered=all(trading_hours(datetime.fromtimestamp(a,timezone.utc),datetime.fromtimestamp(b,timezone.utc))<=5/60 for a,b in zip(timestamps,timestamps[1:]))
            if touched:
                status,outcome='observed_touch',1
            elif covered:
                # Point samples cannot rule out a brief between-sample touch.
                status,outcome='no_touch_observed',0
            else:
                status,outcome='insufficient_coverage',None
            db.execute('UPDATE oil_outcomes SET outcome=?,status=? WHERE forecast_id=? AND hours=?',(outcome,status,fid,hours))


def statistics(now=None):
    now = now or datetime.now(timezone.utc)
    evaluate(now)
    with connect() as db:
        count=db.execute('SELECT COUNT(*) FROM tvc_observations').fetchone()[0]
        forecasts=db.execute('SELECT COUNT(*) FROM oil_forecasts').fetchone()[0]
        news=db.execute('SELECT COUNT(*) FROM oil_news').fetchone()[0]
        historical=db.execute('SELECT COUNT(*) FROM oil_historical').fetchone()[0]
        statuses=dict(db.execute('SELECT status,COUNT(*) FROM oil_outcomes GROUP BY status').fetchall())
        rows=db.execute('SELECT hours,probability,outcome FROM oil_outcomes WHERE outcome IS NOT NULL').fetchall()
    groups={}
    for hours,p,y in rows:
        groups.setdefault(hours,[]).append((p,y))
    return {'backend':'postgresql' if os.getenv('DATABASE_URL') else 'sqlite', 'connected':True,
            'historical_observations':historical,'observations':count,'forecasts':forecasts,'news_articles':news,'outcomes':statuses,
            'observation_based_brier_by_horizon':[{'hours':h,'count':len(v),'score':sum((p-y)**2 for p,y in v)/len(v)} for h,v in sorted(groups.items())],
            'limitations':'Observation-based diagnostics, not validated continuous-touch accuracy. Brief touches may be missed, gaps are excluded, overlapping forecasts are correlated. No hit-rate claim before adequate independent out-of-sample data.',
            'collection':'Only while the service is active; Render free sleep interrupts collection.'}
