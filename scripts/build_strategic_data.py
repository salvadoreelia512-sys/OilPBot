"""Rebuild auditable free daily TVC:USOIL and official EIA weekly data.
Run with the project's Python environment; xlrd reads EIA's published XLS files.
"""
import argparse
import sys
import hashlib
import secrets
import time
import json
from datetime import date, datetime, timezone, timedelta, time as clocktime
from pathlib import Path

import httpx
import pandas as pd
from zoneinfo import ZoneInfo
from websockets.sync.client import connect
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.live_quotes import encode, frames

DEST = Path(__file__).resolve().parents[1] / 'src/data/strategic.json'
SOURCES = {
    'prices': 'TVC:USOIL public widget daily OHLC; resolved identity TVC:USOIL; New York 18:00–17:00 sessions',
    'inventories': 'https://www.eia.gov/dnav/pet/hist_xls/WCESTUS1w.xls',
    'production': 'https://www.eia.gov/dnav/pet/hist_xls/WCRFPUS2w.xls',
}


def fetch_daily(path):
    url='wss://widgetdata.tradingview.com/socket.io/websocket?from=embed-widget%2Fadvanced-chart%2F&api_key=tradingview'
    with connect(url,origin='https://www.tradingview-widget.com',open_timeout=10,close_timeout=2,max_size=5*1024*1024) as socket:
        session='cs_'+secrets.token_hex(6);identity=None;bars=None
        for method,params in [('chart_create_session',[session,'']),
              ('resolve_symbol',[session,'symbol_1','={"symbol":"TVC:USOIL","adjustment":"splits","session":"regular"}']),
              ('create_series',[session,'s1','s1','symbol_1','1D',5000])]:
            socket.send(encode(method,params))
        until=time.monotonic()+30
        while time.monotonic()<until:
            for body in frames(socket.recv(timeout=min(10,max(.1,until-time.monotonic())))):
                if body.startswith('~h~'):
                    socket.send(f'~m~{len(body)}~m~{body}');continue
                message=json.loads(body)
                if message.get('m') in ('critical_error','protocol_error','symbol_error','series_error'):
                    raise ValueError('Historical source refused request')
                if message.get('m')=='symbol_resolved':
                    info=message['p'][2]
                    identity={k:info.get(k) for k in ('name','full_name','pro_name','exchange','timezone','session')}
                    if identity['pro_name']!='TVC:USOIL' or identity['timezone']!='America/New_York' or identity['session']!='1800-1700':
                        raise ValueError('Unexpected historical instrument/session')
                if message.get('m')=='timescale_update':
                    bars=message['p'][1].get('s1',{}).get('s',[])
                if identity and bars:
                    path.write_text(json.dumps({'identity':identity,'bars':bars}))
                    return
        raise ValueError('Historical bars unavailable')


def build(refresh=False, destination=DEST):
    built_at = datetime.now(timezone.utc)
    end = built_at.date().isoformat()
    price_path = Path('/tmp/oilpbot-tvc-daily-source.json')
    if refresh or not price_path.exists():
        fetch_daily(price_path)
    source=json.loads(price_path.read_text())
    if source['identity']['pro_name']!='TVC:USOIL' or source['identity']['timezone']!='America/New_York' or source['identity']['session']!='1800-1700':
        raise ValueError('Wrong historical instrument')
    bars = []
    for row in source['bars']:
        raw=row['v']
        opened=datetime.fromtimestamp(float(raw[0]),timezone.utc).astimezone(ZoneInfo('America/New_York'))
        if opened.hour!=18 or opened.minute!=0:
            raise ValueError('Unexpected daily session timestamp')
        day=(opened.date()+timedelta(days=1)).isoformat()
        values={key:float(value) for key,value in zip(('open','high','low','close'),raw[1:5])}
        # Never use the still-open current session, even if the provider returns it.
        closed=datetime.combine(date.fromisoformat(day),clocktime(17),ZoneInfo('America/New_York')).astimezone(timezone.utc)
        if day>='2010-01-01' and closed<built_at and all(pd.notna(v) for v in values.values()):
            if not values['low'] <= min(values['open'],values['close']) <= max(values['open'],values['close']) <= values['high']:
                raise ValueError('Invalid source OHLC')
            bars.append({'date':day, **values})
    if len(bars) < 3000 or len({b['date'] for b in bars}) != len(bars):
        raise ValueError('Insufficient or duplicate TVC history')
    weekly = {}
    fingerprints = {'prices':hashlib.sha256(price_path.read_bytes()).hexdigest()}
    for name in ('inventories', 'production'):
        path = Path('/tmp/oilpbot-'+name+'.xls')
        if refresh or not path.exists():
            response = httpx.get(SOURCES[name], timeout=30, follow_redirects=True)
            response.raise_for_status()
            path.write_bytes(response.content)
        frame = pd.read_excel(path, sheet_name=1, header=2).iloc[:,:2].dropna()
        rows = [{'date':pd.Timestamp(row[0]).strftime('%Y-%m-%d'), 'value':float(row[1])}
                for row in frame.itertuples(index=False, name=None)
                if '2009-01-01' <= pd.Timestamp(row[0]).strftime('%Y-%m-%d') < end]
        if len(rows) < 500 or any(r['value'] <= 0 for r in rows):
            raise ValueError('Incomplete EIA history')
        weekly[name] = sorted(rows, key=lambda r:r['date'])
        fingerprints[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    result = {'built_at_utc':datetime.now(timezone.utc).isoformat(), 'instrument':'TVC:USOIL','session_identity':source['identity'], 'bars':sorted(bars,key=lambda r:r['date']),
              'weekly':weekly, 'sources':SOURCES, 'source_sha256':fingerprints,
              'limitations':['Historical TVC:USOIL provider bars are not point-in-time vintages; provider revisions and feed methodology can change.',
                             'Downloaded history is revised data, not point-in-time vintages.',
                             'Weekly EIA values use a conservative seven-calendar-day publication lag.',
                             'Daily-origin high/low touch labels do not validate hourly or arbitrary live intraday-origin probabilities.']}
    Path(destination).write_text(json.dumps(result,separators=(',',':'),allow_nan=False)+'\n')
    print({'bars':len(bars),'last_bar':bars[-1]['date'],'weekly':{k:len(v) for k,v in weekly.items()}})
    return result


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--refresh',action='store_true')
    build(parser.parse_args().refresh)
