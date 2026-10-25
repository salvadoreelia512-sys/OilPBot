from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd
import pytest

from src.backtest import BacktestConfig
from src.config import BotSettings
from src.news import (
    EventScorer,
    HeadlineClusterer,
    NewsRiskAdjuster,
    RSSPoller,
    RawHeadline,
    evaluate_news_layer_ab,
)


def _generate_intraday_shock_sessions(
    n_sessions: int = 24,
    bars_per_session: int = 240,
    seed: int = 2026,
) -> tuple[list[pd.DataFrame], list[RawHeadline]]:
    """
    Generates sessions that start quietly (sigma=0.28) up to bar 60, where even-indexed
    sessions experience a breaking news shock at bar 58-59 that shifts forward volatility
    for bars 60..240 to sigma=0.45.
    """
    rng = np.random.default_rng(seed)
    sub_ticks = 10
    dt_sub = 1.0 / (252.0 * 1380.0 * sub_ticks)
    base_date = datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)

    sessions: list[pd.DataFrame] = []
    headlines: list[RawHeadline] = []
    current_price = 75.0

    for s_idx in range(n_sessions):
        session_start = base_date + timedelta(days=s_idx)
        is_shock = (s_idx >= 1) and (s_idx % 2 == 0)
        shock_bar = 60

        sigmas = np.full(bars_per_session * sub_ticks, 0.28)
        if is_shock:
            sigmas[shock_bar * sub_ticks :] = 0.45
            headlines.append(
                RawHeadline(
                    title=f"Urgent: Oil tanker seized in Strait of Hormuz escalation day {s_idx}",
                    source="Reuters",
                    published_utc=session_start + timedelta(minutes=shock_bar - 2),
                )
            )
            headlines.append(
                RawHeadline(
                    title=f"Emergency OPEC+ supply disruption alert day {s_idx}",
                    source="Bloomberg",
                    published_utc=session_start + timedelta(minutes=shock_bar - 1),
                )
            )

        sigma_sub = sigmas * np.sqrt(dt_sub)
        drift_sub = -0.5 * (sigmas ** 2) * dt_sub
        shocks = drift_sub + sigma_sub * rng.standard_normal(bars_per_session * sub_ticks)
        log_path = np.log(current_price) + np.cumsum(shocks)
        prices_sub = np.exp(log_path).reshape(bars_per_session, sub_ticks)

        opens = np.empty(bars_per_session)
        opens[0] = current_price
        opens[1:] = prices_sub[:-1, -1]
        closes = prices_sub[:, -1]
        highs = np.maximum(opens, np.max(prices_sub, axis=1))
        lows = np.minimum(opens, np.min(prices_sub, axis=1))
        current_price = float(closes[-1])

        idx = pd.date_range(session_start, periods=bars_per_session, freq="1min", tz="UTC")
        sessions.append(
            pd.DataFrame(
                {"open": opens, "high": highs, "low": lows, "close": closes},
                index=idx,
            )
        )

    return sessions, headlines


def test_rss_xml_parsing_and_poller_rate_limit():
    sample_xml = """<?xml version="1.0" encoding="UTF-8" ?>
    <rss version="2.0">
      <channel>
        <title>Energy Wire</title>
        <item>
          <title>OPEC+ considers surprise oil production cut amid demand concerns</title>
          <link>https://example.com/opec-cut</link>
          <description>Ministers meet in Vienna to discuss quotas.</description>
          <source>Reuters</source>
          <pubDate>Sun, 25 Oct 2026 10:00:00 GMT</pubDate>
        </item>
      </channel>
    </rss>
    """
    parsed = RSSPoller.parse_rss_xml(sample_xml)
    assert len(parsed) == 1
    assert parsed[0].source == "Reuters"
    assert "OPEC+" in parsed[0].title


def test_40_syndicated_outlets_dedupe_into_single_event():
    base_time = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    clusterer = HeadlineClusterer(similarity_threshold=0.40)

    variations = [
        "Iran navy seizes oil tanker in Strait of Hormuz amid escalation",
        "Breaking: Oil tanker seized by Iran navy in Strait of Hormuz",
        "Crude tanker detained in Strait of Hormuz by Iranian navy forces",
        "Iranian navy boards and seizes commercial oil vessel in Strait of Hormuz",
    ]
    headlines = [
        RawHeadline(
            title=variations[i % len(variations)],
            source=f"outlet_{i}",
            published_utc=base_time + timedelta(seconds=i * 15),
        )
        for i in range(40)
    ]

    clusters = clusterer.ingest(headlines)
    assert len(clusters) == 1
    assert clusters[0].outlet_count == 40
    assert len(clusters[0].unique_sources) == 40


def test_event_scorer_and_half_life_decay_rules():
    t0 = datetime(2026, 10, 25, 8, 0, tzinfo=timezone.utc)
    clusterer = HeadlineClusterer()
    scorer = EventScorer()

    clusters = clusterer.ingest(
        [
            RawHeadline(
                title="Urgent: Missile attack hits oil tanker in Strait of Hormuz",
                source="Reuters",
                published_utc=t0,
            ),
            RawHeadline(
                title="OPEC minister says oil output quota likely unchanged",
                source="Bloomberg",
                published_utc=t0,
            ),
        ]
    )
    assert len(clusters) == 2

    hormuz_scored = scorer.score_cluster(clusters[0])
    opec_scored = scorer.score_cluster(clusters[1])

    assert hormuz_scored.event_type == "hormuz_escalation"
    assert hormuz_scored.half_life_hours == pytest.approx(6.0)
    assert opec_scored.event_type == "opec_comment"
    assert opec_scored.half_life_hours == pytest.approx(2.0)
    assert hormuz_scored.severity > opec_scored.severity

    t_plus_6h = t0 + timedelta(hours=6)
    assert hormuz_scored.decay_factor(t_plus_6h) == pytest.approx(0.50)
    assert opec_scored.decay_factor(t_plus_6h) == pytest.approx(0.125)


def test_risk_adjuster_why_panel_and_decay():
    t0 = datetime(2026, 10, 25, 10, 0, tzinfo=timezone.utc)
    adjuster = NewsRiskAdjuster()
    adjuster.ingest_headlines(
        [
            RawHeadline(
                title="Emergency: Oil tanker seized after explosion in Strait of Hormuz",
                source="Reuters",
                published_utc=t0,
            ),
            RawHeadline(
                title="Oil vessel seized following blast in Strait of Hormuz",
                source="Bloomberg",
                published_utc=t0 + timedelta(minutes=2),
            ),
        ]
    )

    adj_now = adjuster.compute_adjustment(now_utc=t0 + timedelta(minutes=5))
    assert adj_now.active_event_count == 1
    assert adj_now.vol_multiplier > 1.15
    assert adj_now.jump_intensity_annual > 5.0
    assert len(adj_now.drivers) == 1
    assert adj_now.drivers[0].outlet_count == 2
    assert adj_now.drivers[0].event_type == "hormuz_escalation"

    adj_6h = adjuster.compute_adjustment(now_utc=t0 + timedelta(hours=6, minutes=2))
    assert adj_6h.vol_multiplier < adj_now.vol_multiplier
    assert adj_6h.jump_intensity_annual < adj_now.jump_intensity_annual


def test_ab_calibration_gate_keeps_informative_news_and_drops_noise(tmp_path):
    sessions, headlines = _generate_intraday_shock_sessions(
        n_sessions=24,
        bars_per_session=240,
        seed=2026,
    )
    good_adjuster = NewsRiskAdjuster()
    good_adjuster.ingest_headlines(headlines)

    settings = BotSettings(
        sqlite_db_path=str(tmp_path / "ab_news_good.db"),
        default_ovx_level=28.0,
    )
    bt_cfg = BacktestConfig(
        lookback_bars=180,
        session_bars=240,
        checkpoint_fractions=(0.25,),
        target_z_multipliers=(0.60, 0.95, 1.30, 1.65),
        mc_paths=8_000,
    )

    base_sum, news_sum, kept = evaluate_news_layer_ab(
        sessions=sessions,
        news_adjuster=good_adjuster,
        settings=settings,
        config=bt_cfg,
        ovx_index=28.0,
        seed=42,
    )

    assert kept is True
    assert good_adjuster.enabled is True
    assert news_sum.brier_score < base_sum.brier_score
    assert news_sum.expected_calibration_error < base_sum.expected_calibration_error

    # Now test a noisy false-alarm news adjuster on the same sessions (triggers on quiet odd days)
    bad_adjuster = NewsRiskAdjuster()
    base_date = datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)
    for s_idx in range(1, 24, 2):
        session_start = base_date + timedelta(days=s_idx)
        bad_adjuster.ingest_headlines(
            [
                RawHeadline(
                    title=f"Fake alarm: Massive war blockade and explosion in Strait of Hormuz day {s_idx}",
                    source="Reuters",
                    published_utc=session_start + timedelta(minutes=58),
                ),
                RawHeadline(
                    title=f"Urgent emergency embargo and refinery shutdown day {s_idx}",
                    source="Bloomberg",
                    published_utc=session_start + timedelta(minutes=59),
                ),
            ]
        )

    settings_bad = BotSettings(
        sqlite_db_path=str(tmp_path / "ab_news_bad.db"),
        default_ovx_level=28.0,
    )
    _, bad_news_sum, kept_bad = evaluate_news_layer_ab(
        sessions=sessions,
        news_adjuster=bad_adjuster,
        settings=settings_bad,
        config=bt_cfg,
        ovx_index=28.0,
        seed=42,
    )
    assert kept_bad is False
    assert bad_adjuster.enabled is False
