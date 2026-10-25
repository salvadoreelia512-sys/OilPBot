import hashlib
import math
import re
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Dict, List, Literal, Optional, Sequence, Set

import json
import urllib.parse
import urllib.request

try:
    import httpx
except ImportError:
    httpx = None
import pandas as pd

from src.backtest import BacktestConfig, WalkForwardBacktester
from src.calibration import CalibrationSummary, PredictionLogRepository
from src.config import BotSettings

EventType = Literal[
    "hormuz_escalation",
    "supply_disruption",
    "opec_comment",
    "eia_inventory",
    "macro_demand",
    "general_oil",
]

# Half-life in hours by event type (per architecture specification)
EVENT_HALF_LIFE_HOURS: Dict[EventType, float] = {
    "hormuz_escalation": 6.0,
    "supply_disruption": 4.0,
    "opec_comment": 2.0,
    "eia_inventory": 1.5,
    "macro_demand": 2.0,
    "general_oil": 1.0,
}

# Base volatility and jump weights by event type
EVENT_VOL_WEIGHT: Dict[EventType, float] = {
    "hormuz_escalation": 0.65,
    "supply_disruption": 0.50,
    "opec_comment": 0.35,
    "eia_inventory": 0.30,
    "macro_demand": 0.22,
    "general_oil": 0.12,
}

EVENT_JUMP_WEIGHT: Dict[EventType, float] = {
    "hormuz_escalation": 38.0,
    "supply_disruption": 26.0,
    "opec_comment": 16.0,
    "eia_inventory": 14.0,
    "macro_demand": 8.0,
    "general_oil": 3.0,
}

STOPWORDS: Set[str] = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "he",
    "in", "is", "it", "its", "of", "on", "that", "the", "to", "was", "were",
    "will", "with", "after", "over", "amid", "says", "said", "report", "reports",
    "news", "update", "breaking", "oil", "crude", "prices", "price", "market",
    "markets", "barrel", "barrels", "brent", "wti",
}

SYNONYM_MAP: Dict[str, str] = {
    "strait": "hormuz",
    "persian": "hormuz",
    "gulf": "hormuz",
    "tankers": "tanker",
    "vessels": "tanker",
    "ship": "tanker",
    "ships": "tanker",
    "shipping": "tanker",
    "seize": "seized",
    "seizes": "seized",
    "seizure": "seized",
    "hijack": "seized",
    "hijacked": "seized",
    "detained": "seized",
    "boarding": "seized",
    "boarded": "seized",
    "missiles": "missile",
    "drones": "drone",
    "strikes": "strike",
    "attacked": "attack",
    "attacks": "attack",
    "explosions": "explosion",
    "blast": "explosion",
    "cuts": "cut",
    "cutting": "cut",
    "curbs": "cut",
    "quotas": "quota",
    "inventories": "inventory",
    "stockpiles": "inventory",
    "stocks": "inventory",
    "sanction": "sanctions",
    "embargoes": "embargo",
    "outages": "outage",
    "halted": "halt",
    "halts": "halt",
    "shut": "shutdown",
}

ALARM_TERMS: Set[str] = {
    "attack", "seized", "blockade", "explosion", "missile", "drone", "strike",
    "emergency", "embargo", "halt", "shutdown", "force", "majeure", "war",
    "escalation", "retaliation", "crisis", "threat", "closure", "sank", "fire",
}

SURPRISE_TERMS: Set[str] = {
    "unexpected", "surprise", "sudden", "shock", "unscheduled", "emergency",
    "abrupt", "unprecedented", "defies", "contrary", "flash", "urgent",
}

SOURCE_CREDIBILITY_TIERS: Dict[str, float] = {
    "reuters": 0.95,
    "bloomberg": 0.95,
    "associated press": 0.92,
    "ap": 0.92,
    "wall street journal": 0.92,
    "wsj": 0.92,
    "financial times": 0.90,
    "ft": 0.90,
    "eia": 0.98,
    "opec": 0.95,
    "cnbc": 0.82,
    "marketwatch": 0.80,
    "yahoo": 0.78,
    "finnhub": 0.80,
    "gdelt": 0.72,
    "rss": 0.75,
}


@dataclass(frozen=True)
class RawHeadline:
    title: str
    source: str
    published_utc: datetime
    url: str = ""
    summary: str = ""
    headline_id: str = ""

    def __post_init__(self) -> None:
        if not self.headline_id:
            digest = hashlib.sha1(
                f"{self.title.strip().lower()}|{self.source.strip().lower()}".encode("utf-8")
            ).hexdigest()[:12]
            object.__setattr__(self, "headline_id", digest)


@dataclass
class NewsEventCluster:
    cluster_id: str
    canonical_title: str
    first_seen_utc: datetime
    last_seen_utc: datetime
    headlines: List[RawHeadline] = field(default_factory=list)
    tokens: Set[str] = field(default_factory=set)

    @property
    def outlet_count(self) -> int:
        return len(self.headlines)

    @property
    def unique_sources(self) -> Set[str]:
        return {h.source.strip().lower() for h in self.headlines if h.source}


@dataclass(frozen=True)
class ScoredNewsEvent:
    cluster_id: str
    canonical_title: str
    event_type: EventType
    first_seen_utc: datetime
    last_seen_utc: datetime
    outlet_count: int
    unique_source_count: int
    severity: float        # [0.0, 1.0]
    surprise: float        # [0.0, 1.0]
    credibility: float     # [0.0, 1.0]
    half_life_hours: float

    def decay_factor(self, now_utc: datetime) -> float:
        age_hours = max(0.0, (now_utc - self.last_seen_utc).total_seconds() / 3600.0)
        if self.half_life_hours <= 0:
            return 0.0
        return float(math.pow(0.5, age_hours / self.half_life_hours))

    def effective_impact(self, now_utc: datetime) -> float:
        decay = self.decay_factor(now_utc)
        raw_score = self.severity * (0.5 + 0.5 * self.surprise) * self.credibility
        return float(raw_score * decay)


@dataclass(frozen=True)
class RiskDriverItem:
    cluster_id: str
    event_type: EventType
    title: str
    outlet_count: int
    age_minutes: float
    half_life_hours: float
    decay_factor: float
    severity: float
    surprise: float
    credibility: float
    effective_impact: float
    vol_contribution_pct: float
    jump_contribution_annual: float


@dataclass(frozen=True)
class RiskAdjustment:
    vol_multiplier: float
    jump_intensity_annual: float
    headline_velocity_per_hr: float
    active_event_count: int
    drivers: List[RiskDriverItem]


# ============================================================================
# 1. POLLERS (GDELT 15m, RSS 30-60s, Finnhub rate-limited)
# ============================================================================


class NewsPoller(ABC):
    name: str
    min_poll_interval_seconds: float

    def __init__(self, min_poll_interval_seconds: float = 60.0, timeout: float = 4.0):
        self.min_poll_interval_seconds = min_poll_interval_seconds
        self.timeout = timeout
        self._last_poll_utc: Optional[datetime] = None
        self._cached_headlines: List[RawHeadline] = []

    def poll(self, now_utc: Optional[datetime] = None, force: bool = False) -> List[RawHeadline]:
        ref_now = now_utc or datetime.now(timezone.utc)
        if (
            not force
            and self._last_poll_utc is not None
            and (ref_now - self._last_poll_utc).total_seconds() < self.min_poll_interval_seconds
        ):
            return list(self._cached_headlines)

        fresh = self._fetch(ref_now)
        self._last_poll_utc = ref_now
        self._cached_headlines = fresh
        return list(fresh)

    @abstractmethod
    def _fetch(self, now_utc: datetime) -> List[RawHeadline]:
        """Fetches raw headlines from the external source."""


class GDELTPoller(NewsPoller):
    """Polls GDELT 2.0 Doc API every 15 minutes for oil/Hormuz/OPEC events."""
    name = "gdelt"

    def __init__(
        self,
        query: str = '(oil OR crude OR "Strait of Hormuz" OR OPEC OR tanker) sourcelang:english',
        min_poll_interval_seconds: float = 900.0,
        timeout: float = 5.0,
    ):
        super().__init__(min_poll_interval_seconds=min_poll_interval_seconds, timeout=timeout)
        self.query = query

    def _fetch(self, now_utc: datetime) -> List[RawHeadline]:
        url = "https://api.gdeltproject.org/api/v2/doc/doc"
        params = {
            "query": self.query,
            "mode": "ArtList",
            "maxrecords": "50",
            "timespan": "1h",
            "format": "json",
        }
        if httpx is not None:
            resp = httpx.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        else:
            full_url = f"{url}?{urllib.parse.urlencode(params)}"
            with urllib.request.urlopen(full_url, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        articles = data.get("articles", [])
        results: List[RawHeadline] = []
        for art in articles:
            title = (art.get("title") or "").strip()
            if not title:
                continue
            seendate = art.get("seendate", "")
            try:
                dt = datetime.strptime(seendate, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                dt = now_utc
            domain = (art.get("domain") or "gdelt").strip()
            results.append(
                RawHeadline(
                    title=title,
                    source=domain,
                    published_utc=dt,
                    url=art.get("url", ""),
                )
            )
        return results


class RSSPoller(NewsPoller):
    """Polls RSS/Atom feeds every 30 to 60 seconds using standard library XML parsing."""
    name = "rss"

    def __init__(
        self,
        feed_urls: Sequence[str],
        min_poll_interval_seconds: float = 45.0,
        timeout: float = 4.0,
    ):
        super().__init__(min_poll_interval_seconds=min_poll_interval_seconds, timeout=timeout)
        self.feed_urls = list(feed_urls)

    @staticmethod
    def parse_rss_xml(xml_text: str, default_source: str = "rss", fallback_utc: Optional[datetime] = None) -> List[RawHeadline]:
        ref_now = fallback_utc or datetime.now(timezone.utc)
        root = ET.fromstring(xml_text.strip())
        headlines: List[RawHeadline] = []

        # Standard RSS 2.0 <item> elements
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            link = (item.findtext("link") or "").strip()
            desc = (item.findtext("description") or "").strip()
            source_el = item.find("source")
            source_name = (
                source_el.text.strip()
                if (source_el is not None and source_el.text)
                else default_source
            )
            pub_str = (item.findtext("pubDate") or "").strip()
            dt = ref_now
            if pub_str:
                try:
                    dt = parsedate_to_datetime(pub_str).astimezone(timezone.utc)
                except Exception:
                    dt = ref_now
            headlines.append(
                RawHeadline(
                    title=title,
                    source=source_name,
                    published_utc=dt,
                    url=link,
                    summary=desc,
                )
            )
        return headlines

    def _fetch(self, now_utc: datetime) -> List[RawHeadline]:
        collected: List[RawHeadline] = []
        for url in self.feed_urls:
            if httpx is not None:
                resp = httpx.get(url, timeout=self.timeout)
                resp.raise_for_status()
                xml_text = resp.text
            else:
                with urllib.request.urlopen(url, timeout=self.timeout) as r:
                    xml_text = r.read().decode("utf-8")
            collected.extend(self.parse_rss_xml(xml_text, default_source="rss", fallback_utc=now_utc))
        return collected


class FinnhubNewsPoller(NewsPoller):
    """Polls Finnhub general/energy news within rate limits."""
    name = "finnhub_news"

    def __init__(
        self,
        api_key: str,
        min_poll_interval_seconds: float = 60.0,
        timeout: float = 4.0,
    ):
        super().__init__(min_poll_interval_seconds=min_poll_interval_seconds, timeout=timeout)
        self.api_key = api_key

    def _fetch(self, now_utc: datetime) -> List[RawHeadline]:
        if not self.api_key:
            raise ValueError("Finnhub API key not configured")
        url = "https://finnhub.io/api/v1/news"
        params = {"category": "general", "token": self.api_key}
        if httpx is not None:
            resp = httpx.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            items = resp.json()
        else:
            full_url = f"{url}?{urllib.parse.urlencode(params)}"
            with urllib.request.urlopen(full_url, timeout=self.timeout) as r:
                items = json.loads(r.read().decode("utf-8"))
        results: List[RawHeadline] = []
        for item in items:
            title = (item.get("headline") or "").strip()
            summary = (item.get("summary") or "").strip()
            combined = f"{title} {summary}".lower()
            if not any(
                kw in combined
                for kw in ("oil", "crude", "opec", "hormuz", "tanker", "energy", "eia", "brent", "wti")
            ):
                continue
            ts = int(item.get("datetime", 0))
            dt = (
                datetime.fromtimestamp(ts, tz=timezone.utc)
                if ts > 0
                else now_utc
            )
            results.append(
                RawHeadline(
                    title=title,
                    source=item.get("source") or "finnhub",
                    published_utc=dt,
                    url=item.get("url") or "",
                    summary=summary,
                )
            )
        return results


# ============================================================================
# 2. DEDUPLICATION & EVENT CLUSTERING
# ============================================================================


def tokenize_headline(text: str) -> Set[str]:
    """
    Normalizes a headline into canonical content tokens + synonym roots so that
    syndicated rewrites of the same story share high token overlap.
    """
    words = re.findall(r"[a-z0-9]+", text.lower())
    tokens: Set[str] = set()
    for w in words:
        if len(w) <= 2 or w in STOPWORDS:
            continue
        canonical = SYNONYM_MAP.get(w, w)
        if canonical not in STOPWORDS:
            tokens.add(canonical)
    return tokens


def token_similarity(tokens_a: Set[str], tokens_b: Set[str]) -> float:
    """Computes Sorensen-Dice / Jaccard hybrid similarity in [0.0, 1.0]."""
    if not tokens_a or not tokens_b:
        return 0.0
    inter = len(tokens_a & tokens_b)
    if inter == 0:
        return 0.0
    dice = (2.0 * inter) / (len(tokens_a) + len(tokens_b))
    return float(dice)


class HeadlineClusterer:
    """
    Deduplicates and clusters incoming headlines into distinct NewsEventClusters
    so a story repeated by 40 outlets is counted as 1 event.
    """

    def __init__(
        self,
        similarity_threshold: float = 0.42,
        max_cluster_window_hours: float = 8.0,
    ):
        self.similarity_threshold = similarity_threshold
        self.max_cluster_window_hours = max_cluster_window_hours
        self.clusters: List[NewsEventCluster] = []
        self._seen_headline_ids: Set[str] = set()

    def ingest(self, headlines: Sequence[RawHeadline]) -> List[NewsEventCluster]:
        sorted_items = sorted(headlines, key=lambda h: h.published_utc)
        for h in sorted_items:
            if h.headline_id in self._seen_headline_ids:
                continue
            self._seen_headline_ids.add(h.headline_id)

            h_tokens = tokenize_headline(h.title)
            if not h_tokens:
                continue

            best_cluster: Optional[NewsEventCluster] = None
            best_sim = 0.0

            for cluster in self.clusters:
                age_diff_hours = abs(
                    (h.published_utc - cluster.last_seen_utc).total_seconds()
                ) / 3600.0
                if age_diff_hours > self.max_cluster_window_hours:
                    continue

                sim = token_similarity(h_tokens, cluster.tokens)
                if sim >= self.similarity_threshold and sim > best_sim:
                    best_sim = sim
                    best_cluster = cluster

            if best_cluster is not None:
                best_cluster.headlines.append(h)
                if h.published_utc > best_cluster.last_seen_utc:
                    best_cluster.last_seen_utc = h.published_utc
                if h.published_utc < best_cluster.first_seen_utc:
                    best_cluster.first_seen_utc = h.published_utc
                best_cluster.tokens |= h_tokens
            else:
                cid = hashlib.sha1(
                    f"{h.title.lower()}|{int(h.published_utc.timestamp())}".encode("utf-8")
                ).hexdigest()[:10]
                new_cluster = NewsEventCluster(
                    cluster_id=cid,
                    canonical_title=h.title,
                    first_seen_utc=h.published_utc,
                    last_seen_utc=h.published_utc,
                    headlines=[h],
                    tokens=set(h_tokens),
                )
                self.clusters.append(new_cluster)

        return list(self.clusters)

    def prune_older_than(self, cutoff_utc: datetime) -> None:
        self.clusters = [c for c in self.clusters if c.last_seen_utc >= cutoff_utc]


# ============================================================================
# 3. EVENT SCORER (LLM / FinBERT interface + Deterministic Financial Lexicon)
# ============================================================================

CustomScorerFn = Callable[[NewsEventCluster], ScoredNewsEvent]


class EventScorer:
    """
    Scores each deduplicated NewsEventCluster into:
      - event_type
      - severity (0 to 1)
      - surprise (0 to 1)
      - credibility (0 to 1)
      - half_life_hours (by event_type)
    Supports an optional `custom_scorer_fn` hook for external LLM/FinBERT inference.
    """

    def __init__(self, custom_scorer_fn: Optional[CustomScorerFn] = None):
        self.custom_scorer_fn = custom_scorer_fn

    @staticmethod
    def classify_event_type(text: str, tokens: Set[str]) -> EventType:
        lower = text.lower()
        if any(
            k in lower or k in tokens
            for k in ("hormuz", "iran", "navy", "strait", "red sea", "houthi", "seized", "blockade", "tanker")
        ):
            return "hormuz_escalation"
        if any(
            k in lower or k in tokens
            for k in ("pipeline", "refinery", "outage", "sanctions", "embargo", "libya", "force majeure", "explosion", "leak")
        ):
            return "supply_disruption"
        if any(
            k in lower or k in tokens
            for k in ("opec", "opec+", "saudi", "riyadh", "quota", "production cut", "output", "minister")
        ):
            return "opec_comment"
        if any(
            k in lower or k in tokens
            for k in ("eia", "api", "inventory", "stockpile", "cushing", "draw", "build", "spr")
        ):
            return "eia_inventory"
        if any(
            k in lower or k in tokens
            for k in ("fed", "cpi", "inflation", "recession", "china demand", "pmi", "gdp", "tariff")
        ):
            return "macro_demand"
        return "general_oil"

    def score_cluster(
        self,
        cluster: NewsEventCluster,
        recent_event_types: Optional[Sequence[EventType]] = None,
    ) -> ScoredNewsEvent:
        if self.custom_scorer_fn is not None:
            return self.custom_scorer_fn(cluster)

        combined_text = " ".join(
            f"{h.title} {h.summary}" for h in cluster.headlines[:5]
        ).lower()
        all_words = set(re.findall(r"[a-z0-9]+", combined_text))
        norm_words = {SYNONYM_MAP.get(w, w) for w in all_words}

        event_type = self.classify_event_type(combined_text, cluster.tokens | norm_words)
        half_life = EVENT_HALF_LIFE_HOURS[event_type]

        # 1. Severity [0, 1]: base by event type + alarm language hits
        base_sev = {
            "hormuz_escalation": 0.65,
            "supply_disruption": 0.55,
            "opec_comment": 0.45,
            "eia_inventory": 0.40,
            "macro_demand": 0.35,
            "general_oil": 0.20,
        }[event_type]
        alarm_hits = len(norm_words & ALARM_TERMS)
        severity = min(1.0, base_sev + 0.10 * alarm_hits)

        # 2. Surprise [0, 1]: shock/unexpected language + novelty vs recent events
        surprise_hits = len(norm_words & SURPRISE_TERMS)
        novelty_bonus = 0.15
        if recent_event_types and event_type in recent_event_types:
            novelty_bonus = 0.0
        surprise = min(1.0, 0.45 + 0.15 * surprise_hits + novelty_bonus)

        # 3. Credibility [0, 1]: highest tier source in cluster + multi-source corroboration
        best_tier = 0.70
        for src in cluster.unique_sources:
            for known_src, weight in SOURCE_CREDIBILITY_TIERS.items():
                if known_src in src:
                    best_tier = max(best_tier, weight)
        corroboration_boost = min(0.12, 0.04 * math.log1p(max(0, len(cluster.unique_sources) - 1)))
        credibility = min(1.0, best_tier + corroboration_boost)

        return ScoredNewsEvent(
            cluster_id=cluster.cluster_id,
            canonical_title=cluster.canonical_title,
            event_type=event_type,
            first_seen_utc=cluster.first_seen_utc,
            last_seen_utc=cluster.last_seen_utc,
            outlet_count=cluster.outlet_count,
            unique_source_count=len(cluster.unique_sources),
            severity=round(severity, 4),
            surprise=round(surprise, 4),
            credibility=round(credibility, 4),
            half_life_hours=half_life,
        )


# ============================================================================
# 4. RISK ADJUSTER & OUT-OF-SAMPLE A/B CALIBRATION GATE
# ============================================================================


class NewsRiskAdjuster:
    """
    Ingestion -> Clustering -> Scoring -> Half-Life Decay -> RiskAdjustment.
    Produces `vol_multiplier`, `jump_intensity_annual`, and the UI "Why" drivers panel.
    Can be automatically gated on/off by `evaluate_news_layer_ab()`.
    """

    def __init__(
        self,
        settings: Optional[BotSettings] = None,
        clusterer: Optional[HeadlineClusterer] = None,
        scorer: Optional[EventScorer] = None,
        enabled: bool = True,
    ):
        self.settings = settings or BotSettings()
        self.clusterer = clusterer or HeadlineClusterer(
            similarity_threshold=self.settings.news_cluster_similarity_threshold
        )
        self.scorer = scorer or EventScorer()
        self.enabled = enabled

    def ingest_headlines(self, headlines: Sequence[RawHeadline]) -> int:
        clusters = self.clusterer.ingest(headlines)
        return len(clusters)

    def compute_adjustment(self, now_utc: Optional[datetime] = None) -> RiskAdjustment:
        ref_now = now_utc or datetime.now(timezone.utc)
        if not self.enabled:
            return RiskAdjustment(
                vol_multiplier=1.0,
                jump_intensity_annual=0.0,
                headline_velocity_per_hr=0.0,
                active_event_count=0,
                drivers=[],
            )

        # Only consider clusters seen up to `ref_now` (strictly causal for backtesting)
        active_clusters = [
            c
            for c in self.clusterer.clusters
            if c.first_seen_utc <= ref_now
            and (ref_now - c.last_seen_utc).total_seconds() <= 24.0 * 3600.0
        ]

        # Compute headline velocity in the last 60 minutes (fear proxy)
        recent_headline_count = sum(
            1
            for c in active_clusters
            for h in c.headlines
            if 0.0 <= (ref_now - h.published_utc).total_seconds() <= 3600.0
        )
        velocity_per_hr = float(recent_headline_count)
        velocity_boost = min(0.20, 0.02 * max(0.0, velocity_per_hr - 3.0))

        vol_signal_sum = 0.0
        jump_signal_sum = 0.0
        drivers: List[RiskDriverItem] = []
        recent_types: List[EventType] = []

        for cluster in sorted(active_clusters, key=lambda c: c.first_seen_utc):
            # Filter cluster headlines strictly <= ref_now for causal backtesting
            causal_headlines = [h for h in cluster.headlines if h.published_utc <= ref_now]
            if not causal_headlines:
                continue

            causal_cluster = NewsEventCluster(
                cluster_id=cluster.cluster_id,
                canonical_title=causal_headlines[0].title,
                first_seen_utc=causal_headlines[0].published_utc,
                last_seen_utc=causal_headlines[-1].published_utc,
                headlines=causal_headlines,
                tokens=cluster.tokens,
            )

            scored = self.scorer.score_cluster(causal_cluster, recent_event_types=recent_types)
            recent_types.append(scored.event_type)

            decay = scored.decay_factor(ref_now)
            if decay < 0.03:
                continue

            impact = scored.effective_impact(ref_now)
            v_contrib = EVENT_VOL_WEIGHT[scored.event_type] * impact
            j_contrib = EVENT_JUMP_WEIGHT[scored.event_type] * impact

            vol_signal_sum += v_contrib
            jump_signal_sum += j_contrib

            age_mins = max(0.0, (ref_now - scored.last_seen_utc).total_seconds() / 60.0)
            drivers.append(
                RiskDriverItem(
                    cluster_id=scored.cluster_id,
                    event_type=scored.event_type,
                    title=scored.canonical_title,
                    outlet_count=scored.outlet_count,
                    age_minutes=round(age_mins, 1),
                    half_life_hours=scored.half_life_hours,
                    decay_factor=round(decay, 4),
                    severity=scored.severity,
                    surprise=scored.surprise,
                    credibility=scored.credibility,
                    effective_impact=round(impact, 4),
                    vol_contribution_pct=round(v_contrib * 100.0, 2),
                    jump_contribution_annual=round(j_contrib, 2),
                )
            )

        # Sub-additive saturation up to configured caps
        max_extra_vol = max(0.1, self.settings.news_max_vol_multiplier - 1.0)
        total_vol_signal = vol_signal_sum + velocity_boost
        vol_mult = 1.0 + max_extra_vol * (1.0 - math.exp(-total_vol_signal / max_extra_vol))

        max_jump = max(1.0, self.settings.news_max_jump_intensity)
        jump_intensity = max_jump * (1.0 - math.exp(-jump_signal_sum / max_jump))

        drivers.sort(key=lambda d: d.effective_impact, reverse=True)

        return RiskAdjustment(
            vol_multiplier=round(float(vol_mult), 4),
            jump_intensity_annual=round(float(jump_intensity), 4),
            headline_velocity_per_hr=round(velocity_per_hr, 2),
            active_event_count=len(drivers),
            drivers=drivers,
        )

    def as_backtest_hook(self) -> Callable[[pd.Series, datetime], tuple[float, float]]:
        """Returns a `RiskHook` compatible with `WalkForwardBacktester.run_walk_forward()`."""
        def _hook(closes_1m: pd.Series, now_utc: datetime) -> tuple[float, float]:
            adj = self.compute_adjustment(now_utc=now_utc)
            return adj.vol_multiplier, adj.jump_intensity_annual
        return _hook


def evaluate_news_layer_ab(
    sessions: Sequence[pd.DataFrame],
    news_adjuster: NewsRiskAdjuster,
    settings: Optional[BotSettings] = None,
    config: Optional[BacktestConfig] = None,
    ovx_index: float = 32.0,
    seed: int = 42,
) -> tuple[CalibrationSummary, CalibrationSummary, bool]:
    """
    Phase 4 A/B Test Gate:
    Runs `WalkForwardBacktester` on `sessions` first without news (baseline),
    then with `news_adjuster` active.
    If the news-aware model beats the baseline on out-of-sample calibration,
    `news_adjuster.enabled` remains True; otherwise it is automatically disabled.
    """
    cfg_settings = settings or BotSettings()
    base_cfg = config or BacktestConfig(model_version="baseline_v1")
    news_cfg = BacktestConfig(
        model_version="news_aware_v1",
        symbol=base_cfg.symbol,
        lookback_bars=base_cfg.lookback_bars,
        session_bars=base_cfg.session_bars,
        checkpoint_fractions=base_cfg.checkpoint_fractions,
        target_z_multipliers=base_cfg.target_z_multipliers,
        default_leverage=base_cfg.default_leverage,
        default_required_return=base_cfg.default_required_return,
        mc_paths=base_cfg.mc_paths,
    )

    repo = PredictionLogRepository(db_path=cfg_settings.sqlite_db_path)

    # 1. Baseline walk-forward run
    bt_base = WalkForwardBacktester(
        settings=cfg_settings,
        config=base_cfg,
        log_repo=repo,
        seed=seed,
    )
    baseline_summary = bt_base.run_walk_forward(
        sessions=sessions,
        ovx_index=ovx_index,
        risk_hook=None,
        save_as_benchmark=True,
    )

    # 2. News-aware walk-forward run
    news_adjuster.enabled = True
    bt_news = WalkForwardBacktester(
        settings=cfg_settings,
        config=news_cfg,
        log_repo=repo,
        seed=seed,
    )
    news_summary = bt_news.run_walk_forward(
        sessions=sessions,
        ovx_index=ovx_index,
        risk_hook=news_adjuster.as_backtest_hook(),
        save_as_benchmark=False,
    )

    keep_news = news_summary.beats_baseline(baseline_summary)
    news_adjuster.enabled = keep_news
    if keep_news:
        repo.save_benchmark(news_summary)

    return baseline_summary, news_summary, keep_news
