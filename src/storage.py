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


MODEL_VERSION = 'strategic-tvc-v1'


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
    'CREATE TABLE IF NOT EXISTS price_outlook_log (id TEXT PRIMARY KEY, model_version TEXT NOT NULL, created DOUBLE PRECISION NOT NULL, quote_ts DOUBLE PRECISION NOT NULL, evidence TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS technical_signal_log (id TEXT PRIMARY KEY, rule_version TEXT NOT NULL, created DOUBLE PRECISION NOT NULL, quote_ts DOUBLE PRECISION NOT NULL, evidence TEXT NOT NULL)',
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
        rows = db.execute('SELECT ts,price FROM tvc_observations WHERE ts IN (SELECT MAX(ts) FROM tvc_observations WHERE ts<=? GROUP BY CAST(ts AS BIGINT) / 60 ORDER BY MAX(ts) DESC LIMIT 1000) ORDER BY ts DESC', (snapshot.timestamp.timestamp(),)).fetchall()
    return rows[::-1]


def save_forecast(result, now):
    context = result['evidence']['news']
    compact = {'model':result['model'], 'outlook':result['outlook'],
               'observations':result['evidence']['observations'],
               'news_coverage':context['coverage'], 'news_vol_multiplier':context['vol_multiplier'],
               'news_jump_intensity':context['jump_intensity'],
               'news_ids':[], 'quote_price':result['quote']['price']}
    if result.get('strategic'):
        s=result['strategic']
        compact['strategic']={key:s.get(key) for key in ('data_sha256','last_bar','model','fundamentals','risk_assumptions','proxy_validation_promoted')}
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
        rows=db.execute('SELECT f.model_version,o.hours,o.probability,o.outcome FROM oil_outcomes o JOIN oil_forecasts f ON f.id=o.forecast_id WHERE o.outcome IS NOT NULL').fetchall()
    groups={}
    for version,hours,p,y in rows:
        groups.setdefault((version,hours),[]).append((p,y))
    return {'backend':'postgresql' if os.getenv('DATABASE_URL') else 'sqlite', 'connected':True,
            'historical_observations':historical,'observations':count,'forecasts':forecasts,'news_articles':news,'outcomes':statuses,
            'observation_based_brier_by_horizon':[{'model_version':version,'hours':h,'count':len(v),'score':sum((p-y)**2 for p,y in v)/len(v)} for (version,h),v in sorted(groups.items())],
            'limitations':'Observation-based diagnostics, not validated continuous-touch accuracy. Brief touches may be missed, gaps are excluded, overlapping forecasts are correlated. No hit-rate claim before adequate independent out-of-sample data.',
            'collection':'Only while the service is active; Render free sleep interrupts collection.'}


def save_signal(signal, now):
    """Deduplicate state changes and headline sets; never log every live tick."""
    headlines=signal['news'].get('headlines',[])
    news_ids=[hashlib.sha256(json.dumps([h['title'],h['url'],h['published_utc']]).encode()).hexdigest() for h in headlines]
    key=[signal.get('scenario_policy','qualified-rule-v1'),signal['rule_version'],signal['data_sha256'],signal['as_of_bar'],signal['direction'],signal['state'],signal['status'],sorted(signal['blocked_reasons']),news_ids]
    sid=hashlib.sha256(json.dumps(key).encode()).hexdigest()
    compact={k:signal[k] for k in ('rule_version','data_sha256','as_of_bar','weekly_as_of','score','counts','setup','state','status','blocked_reasons','quote','news')}
    compact.update({key:signal.get(key) for key in ('scenario_policy','scenario_basis','rule_direction','rule_eligible')})
    compact['historical_reliability_gate_passed']=signal['historical_validation'].get('reliability_gate_passed',False)
    with connect() as db:
        db.execute('INSERT INTO technical_signal_log VALUES (?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                   (sid,signal['rule_version'],now.timestamp(),datetime.fromisoformat(signal['quote']['timestamp_utc']).timestamp(),json.dumps(compact)))
    return sid


def save_price_outlook(result,now):
    model=result['model']
    outcomes={hours:{key:value for key,value in item.items() if key!='groups'} | {'groups':[{key:value for key,value in group.items() if key!='curve'} for group in item['groups']]} for hours,item in result['outcomes'].items()}
    evidence={'model':model,'quote':result['quote'],'reference_price':result['reference_price'],'outcomes':outcomes,
              'fundamentals':result['fundamentals'],'news':result['news']}
    key=[model['version'],int(now.timestamp()//60),result['quote']['timestamp_utc'],result['reference_price'],model['data_sha256'],model['risk_assumptions']]
    oid=hashlib.sha256(json.dumps(key).encode()).hexdigest()
    with connect() as db:
        db.execute('INSERT INTO price_outlook_log VALUES (?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                   (oid,model['version'],now.timestamp(),datetime.fromisoformat(result['quote']['timestamp_utc']).timestamp(),json.dumps(evidence)))
    return oid
