"""Shared, unauthenticated TVC:USOIL widget quote connection.

This is TradingView's public widget protocol, not a supported paid data API.
Keep source timestamps, fail closed on malformed data, and reconnect on failure.
"""
import json
import math
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from websockets.sync.client import connect

SYMBOL = "TVC:USOIL"
STREAM_URL = "wss://widgetdata.tradingview.com/socket.io/websocket?from=embed-widget%2Fsingle-quote%2F&api_key=tradingview"


def encode(method, params):
    body = json.dumps({"m": method, "p": params}, separators=(",", ":"))
    return f"~m~{len(body)}~m~{body}"


def frames(message):
    """A WebSocket message may contain several length-prefixed protocol frames."""
    while message:
        if not message.startswith("~m~"):
            raise ValueError("Invalid quote framing")
        length, separator, rest = message[3:].partition("~m~")
        if not separator or not length.isdigit() or int(length) > len(rest):
            raise ValueError("Incomplete quote frame")
        size = int(length)
        yield rest[:size]
        message = rest[size:]


@dataclass(frozen=True)
class LiveQuote:
    price: float
    timestamp: datetime
    high: float
    low: float


class QuoteState:
    def __init__(self, session):
        self.session = session
        self.values = {}
        self.latest = None

    def accept(self, message, now):
        if message.get("m") in ("critical_error", "protocol_error"):
            raise ValueError("Quote protocol rejected")
        params = message.get("p", [])
        if message.get("m") != "qsd" or len(params) != 2 or params[0] != self.session:
            return None
        update = params[1]
        if not isinstance(update, dict) or update.get("n") != SYMBOL:
            return None
        if update.get("s") != "ok":
            raise ValueError("Instrument unavailable")
        values = update.get("v", {})
        if not isinstance(values, dict):
            raise ValueError("Invalid quote values")
        self.values.update(values)
        # Never attach an old timestamp to a new price or vice versa.
        if not {"lp", "lp_time"}.issubset(values):
            return None
        try:
            price, high, low = (float(self.values[k]) for k in ("lp", "high_price", "low_price"))
            stamp = datetime.fromtimestamp(float(values["lp_time"]), timezone.utc)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            raise ValueError("Invalid quote fields") from None
        if not all(math.isfinite(v) and v > 0 for v in (price, high, low)) or not low <= price <= high:
            raise ValueError("Invalid quote range")
        if (stamp - now).total_seconds() > 60:
            raise ValueError("Future quote timestamp")
        if self.latest and stamp < self.latest.timestamp:
            return None
        self.latest = LiveQuote(price, stamp, high, low)
        return self.latest


class QuoteStream:
    def __init__(self):
        self.condition = threading.Condition()
        self.latest = None
        self.last_error = None
        self.thread = None
        self.stopping = threading.Event()

    def start(self):
        with self.condition:
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name="tvc-quotes", daemon=True)
                self.thread.start()

    def get(self, timeout=10):
        self.start()
        with self.condition:
            self.condition.wait_for(lambda: self.latest is not None or self.last_error is not None, timeout)
            if self.latest is None:
                raise RuntimeError("TVC:USOIL stream unavailable; reconnecting automatically")
            return self.latest

    def _publish(self, quote):
        with self.condition:
            if self.latest is None or quote.timestamp >= self.latest.timestamp:
                self.latest = quote
                self.last_error = None
                self.condition.notify_all()

    def _run(self):
        retry = 1
        while not self.stopping.is_set():
            try:
                session = "qs_" + secrets.token_hex(6)
                state = QuoteState(session)
                with connect(STREAM_URL, origin="https://www.tradingview-widget.com",
                             open_timeout=8, close_timeout=2, max_size=1024 * 1024) as socket:
                    for method, params in (
                        ("quote_create_session", [session]),
                        ("quote_set_fields", [session, "lp", "lp_time", "high_price", "low_price"]),
                        ("quote_add_symbols", [session, SYMBOL]),
                    ):
                        socket.send(encode(method, params))
                    while not self.stopping.is_set():
                        for body in frames(socket.recv(timeout=30)):
                            if body.startswith("~h~"):
                                socket.send(f"~m~{len(body)}~m~{body}")
                                continue
                            quote = state.accept(json.loads(body), datetime.now(timezone.utc))
                            if quote:
                                self._publish(quote)
                                retry = 1
            except Exception:
                # Never expose network internals or change TLS verification.
                with self.condition:
                    self.last_error = "TVC:USOIL stream disconnected; reconnecting automatically"
                    self.condition.notify_all()
                self.stopping.wait(retry)
                retry = min(30, retry * 2)


stream = QuoteStream()
