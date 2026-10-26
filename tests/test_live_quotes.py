import json
from datetime import datetime, timezone
import pytest
from src.live_quotes import QuoteState, QuoteStream, encode, frames

NOW = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)


def update(values, symbol='TVC:USOIL', session='qs_test', status='ok'):
    return {'m':'qsd','p':[session,{'n':symbol,'s':status,'v':values}]}


def values(**kwargs):
    return {'lp':75,'lp_time':NOW.timestamp(),'high_price':76,'low_price':74,**kwargs}


def test_frames_and_heartbeat_support_multiple_messages():
    packet=encode('qsd',['one'])+encode('quote_completed',['two'])+'~m~4~m~~h~1'
    decoded=list(frames(packet))
    assert json.loads(decoded[0])['m']=='qsd'
    assert json.loads(decoded[1])['m']=='quote_completed'
    assert decoded[2]=='~h~1'
    for invalid in ['invalid','~m~50~m~{}','~m~x~m~{}']:
        with pytest.raises(ValueError):list(frames(invalid))


def test_quote_identity_pairing_and_metadata_updates():
    state=QuoteState('qs_test')
    assert state.accept(update(values(),symbol='NYMEX:CL1!'),NOW) is None
    assert state.accept(update(values(),session='other'),NOW) is None
    first=state.accept(update(values()),NOW)
    assert first.price==75 and first.timestamp==NOW
    # Neither half of an incomplete update invents a price/timestamp pair.
    assert state.accept(update({'lp':75.5}),NOW) is None
    assert state.accept(update({'lp_time':NOW.timestamp()+1}),NOW) is None
    assert state.latest==first
    second=state.accept(update({'lp':75.5,'lp_time':NOW.timestamp()+1}),NOW)
    assert second.price==75.5 and second.high==76 and second.low==74
    assert state.accept(update(values(lp=74.9,lp_time=NOW.timestamp()-1)),NOW) is None
    assert state.latest==second


@pytest.mark.parametrize('bad',[{'lp':float('nan')},{'lp':77},{'lp_time':NOW.timestamp()+61},{'low_price':0}])
def test_invalid_quotes_are_rejected(bad):
    with pytest.raises(ValueError):QuoteState('qs_test').accept(update(values(**bad)),NOW)


def test_instrument_denial_is_not_silently_replaced():
    with pytest.raises(ValueError):QuoteState('qs_test').accept(update({},status='error'),NOW)


def test_shared_quote_reuse_and_no_snapshot_fallback(monkeypatch):
    source=QuoteStream()
    monkeypatch.setattr(source,'start',lambda:None)
    with pytest.raises(RuntimeError):source.get(timeout=0)
    quote=QuoteState('qs_test').accept(update(values()),NOW)
    source._publish(quote)
    assert source.get(timeout=0)==quote
    source.last_error='Disconnected'
    assert source.get(timeout=0)==quote  # Age is checked by the forecast layer.
