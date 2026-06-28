#!/usr/bin/env python3
"""Deterministic tests for the US stock screener."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
import sys
import json
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import us_stock_screener as screener
import backtest_us_stock_screener as backtest
from us_stock_screener import ScreenConfig, build_report, load_records, screen_records
from fetch_yfinance_snapshot import (
    SnapshotBundle,
    build_market_context,
    fetch_snapshot,
    load_market_context,
    load_snapshot,
    load_watchlist,
    save_market_context,
    save_snapshot,
)
from us_stock_screener_web import parse_uploaded_content, run_screen_request
from us_stock_screener_gui import report_to_text as gui_report_to_text


class FakeProvider:
    def __init__(self, payloads):
        self.payloads = payloads

    def fetch(self, ticker):
        value = self.payloads[ticker]
        if isinstance(value, Exception):
            raise value
        return value


class RetryProvider:
    def __init__(self, failures_before_success, success_payload):
        self.failures_before_success = failures_before_success
        self.success_payload = success_payload
        self.calls = 0

    def fetch(self, ticker):
        self.calls += 1
        if self.calls <= self.failures_before_success:
            raise TimeoutError("curl timeout")
        return self.success_payload


class CountingPayloadProvider:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def fetch(self, ticker):
        self.calls += 1
        return self.payload


class ScreenerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = ScreenConfig()

    def _sector_preview_record(self, ticker: str, **overrides):
        record = {
            "ticker": ticker,
            "sector": "Technology",
            "industry": "Software",
            "price": 100,
            "market_cap": 50_000_000_000,
            "avg_dollar_volume_20d": 100_000_000,
            "revenue_growth_yoy": 10,
            "eps_growth_yoy": 10,
            "gross_margin": 45,
            "operating_margin": 20,
            "return_on_equity": 15,
            "pe_ratio": 25,
            "ps_ratio": 6,
            "relative_strength_252d": 70,
            "price_vs_sma50_pct": 3,
            "price_vs_sma200_pct": 6,
            "beta": 1.0,
            "volatility_63d": 25,
            "max_drawdown_252d": 15,
            "data_age_days": 2,
        }
        record.update(overrides)
        return record

    def _market_context(self, regime: str) -> dict:
        if regime == "risk_on":
            return {
                "as_of_date": "2026-06-27",
                "spy_close": 610.0,
                "spy_sma200": 580.0,
                "qqq_close": 530.0,
                "qqq_sma200": 500.0,
                "vix_close": 18.0,
                "breadth_above_200dma": 0.72,
                "breadth_eligible_count": 450,
                "market_context_source": "test",
            }
        if regime == "risk_off":
            return {
                "as_of_date": "2026-06-27",
                "spy_close": 540.0,
                "spy_sma200": 580.0,
                "qqq_close": 460.0,
                "qqq_sma200": 500.0,
                "vix_close": 28.0,
                "breadth_above_200dma": 0.31,
                "breadth_eligible_count": 450,
                "market_context_source": "test",
            }
        if regime == "neutral":
            return {
                "as_of_date": "2026-06-27",
                "spy_close": 590.0,
                "spy_sma200": 580.0,
                "qqq_close": 490.0,
                "qqq_sma200": 500.0,
                "vix_close": 22.0,
                "breadth_above_200dma": 0.52,
                "breadth_eligible_count": 450,
                "market_context_source": "test",
            }
        if regime == "insufficient":
            return {
                "as_of_date": None,
                "spy_close": 590.0,
                "spy_sma200": None,
                "qqq_close": None,
                "qqq_sma200": None,
                "vix_close": 22.0,
                "breadth_above_200dma": None,
                "breadth_eligible_count": 0,
                "market_context_source": "test",
            }
        raise ValueError(regime)

    def _backtest_record(self, ticker: str, price: float, **overrides):
        record = {
            "ticker": ticker,
            "sector": "Technology",
            "industry": "Software",
            "price": price,
            "market_cap": 50_000_000_000,
            "avg_dollar_volume_20d": 100_000_000,
            "revenue_growth_yoy": 10,
            "eps_growth_yoy": 10,
            "gross_margin": 45,
            "operating_margin": 20,
            "return_on_equity": 15,
            "pe_ratio": 25,
            "ps_ratio": 6,
            "relative_strength_252d": 70,
            "price_vs_sma50_pct": 3,
            "price_vs_sma200_pct": 6,
            "beta": 1.0,
            "volatility_63d": 25,
            "max_drawdown_252d": 15,
            "data_age_days": 2,
        }
        record.update(overrides)
        return record

    def _write_backtest_snapshot(self, directory: Path, as_of: str, records, benchmarks=None):
        payload = {
            "metadata": {
                "as_of": as_of,
                "benchmarks": benchmarks or {},
            },
            "records": records,
        }
        path = directory / f"{as_of}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False))
        return path

    def test_yfinance_snapshot_writes_sector_and_industry_metadata(self) -> None:
        provider = FakeProvider(
            {
                "META": {
                    "info": {
                        "sector": " Communication Services ",
                        "industry": " Internet Content ",
                        "regularMarketPrice": 100,
                        "marketCap": 100_000_000_000,
                        "quoteType": "EQUITY",
                    },
                    "history": [{"Date": "2026-06-26", "Close": 100, "Volume": 1_000_000}],
                    "errors": [],
                }
            }
        )
        bundle = fetch_snapshot(["META"], provider=provider, as_of=date(2026, 6, 27))
        record = bundle.records[0]
        self.assertEqual(record["sector"], "Communication Services")
        self.assertEqual(record["industry"], "Internet Content")
        self.assertEqual(bundle.sector_metadata_coverage, 1.0)
        self.assertEqual(bundle.industry_metadata_coverage, 1.0)

    def test_yfinance_snapshot_marks_empty_metadata_missing(self) -> None:
        provider = FakeProvider(
            {
                "MISS": {
                    "info": {
                        "sector": " ",
                        "industry": "",
                        "regularMarketPrice": 100,
                        "marketCap": 100_000_000_000,
                        "quoteType": "EQUITY",
                    },
                    "history": [{"Date": "2026-06-26", "Close": 100, "Volume": 1_000_000}],
                    "errors": [],
                }
            }
        )
        bundle = fetch_snapshot(["MISS"], provider=provider, as_of=date(2026, 6, 27))
        raw = bundle.records[0]["raw"]
        self.assertIsNone(bundle.records[0]["sector"])
        self.assertTrue(raw["metadata_missing"])
        self.assertFalse(raw["metadata_fetch_failed"])
        self.assertEqual(bundle.metadata_missing_count, 1)

    def test_yfinance_snapshot_marks_metadata_fetch_failed(self) -> None:
        provider = FakeProvider(
            {
                "FAIL": {
                    "info": {"__metadata_fetch_failed__": True},
                    "history": [{"Date": "2026-06-26", "Close": 100, "Volume": 1_000_000}],
                    "errors": ["info timeout"],
                }
            }
        )
        bundle = fetch_snapshot(["FAIL"], provider=provider, as_of=date(2026, 6, 27))
        raw = bundle.records[0]["raw"]
        self.assertTrue(raw["metadata_fetch_failed"])
        self.assertFalse(raw["metadata_missing"])
        self.assertEqual(bundle.metadata_fetch_failed_count, 1)

    def test_sector_coverage_below_gate_uses_legacy_as_official(self) -> None:
        records = [
            self._sector_preview_record("HASMETA", sector="Technology", industry="Software"),
            self._sector_preview_record("NOMETA1", sector=None, industry=None, revenue_growth_yoy=30),
            self._sector_preview_record("NOMETA2", sector=None, industry=None, revenue_growth_yoy=1),
        ]
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=3)
        self.assertFalse(report.sector_aware_official_scoring)
        self.assertEqual(report.sector_aware_status, "disabled_insufficient_sector_metadata")
        self.assertTrue(all(item.total_score == item.legacy_total_score for item in report.candidates))
        self.assertTrue(all(item.sector_relative_peer_source == "not_scored_sector_aware_disabled" for item in report.candidates))
        self.assertTrue(all(item.official_score_source == "legacy_metadata_gate" for item in report.candidates))

    def test_sector_coverage_above_gate_uses_sector_aware_official_score(self) -> None:
        records = [
            self._sector_preview_record("LOW", sector="Technology", industry="Software", revenue_growth_yoy=1, eps_growth_yoy=1),
            self._sector_preview_record("HIGH", sector="Technology", industry="Software", revenue_growth_yoy=30, eps_growth_yoy=30),
        ]
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=2)
        self.assertTrue(report.sector_aware_official_scoring)
        self.assertEqual(report.sector_aware_status, "enabled")
        self.assertTrue(any(item.total_score != item.legacy_total_score for item in report.candidates))
        self.assertTrue(all(item.official_score_source == "sector_aware" for item in report.candidates))

    def test_sector_coverage_above_gate_missing_metadata_uses_legacy_official_without_preview_rank_pollution(self) -> None:
        records = [
            self._sector_preview_record(
                "FULLHIGH",
                sector="Technology",
                industry="Software",
                revenue_growth_yoy=28,
                eps_growth_yoy=28,
                gross_margin=58,
                operating_margin=28,
                return_on_equity=24,
                pe_ratio=22,
                ps_ratio=5,
                relative_strength_252d=82,
                price_vs_sma200_pct=8,
                beta=0.9,
                volatility_63d=18,
                max_drawdown_252d=10,
            ),
            self._sector_preview_record(
                "FULLMID",
                sector="Technology",
                industry="Software",
                revenue_growth_yoy=9,
                eps_growth_yoy=9,
                gross_margin=39,
                operating_margin=14,
                return_on_equity=12,
                pe_ratio=27,
                ps_ratio=6.5,
                relative_strength_252d=56,
                price_vs_sma200_pct=2,
                beta=1.05,
                volatility_63d=23,
                max_drawdown_252d=18,
            ),
            self._sector_preview_record(
                "NOMETA",
                sector=None,
                industry=None,
                revenue_growth_yoy=12,
                eps_growth_yoy=12,
                gross_margin=44,
                operating_margin=18,
                return_on_equity=14,
                pe_ratio=26,
                ps_ratio=6,
                relative_strength_252d=72,
                price_vs_sma200_pct=5,
                beta=1.0,
                volatility_63d=24,
                max_drawdown_252d=16,
            ),
        ]
        records.extend(
            self._sector_preview_record(
                f"WEAK{index:02d}",
                sector="Technology",
                industry="Hardware",
                revenue_growth_yoy=-5 + index,
                eps_growth_yoy=-4 + index,
                gross_margin=20 + index,
                operating_margin=5 + index,
                return_on_equity=4 + index,
                pe_ratio=40 + index,
                ps_ratio=9 + index * 0.2,
                relative_strength_252d=20 + index,
                price_vs_sma200_pct=-8 + index * 0.5,
                beta=1.4,
                volatility_63d=38 + index,
                max_drawdown_252d=28 + index,
            )
            for index in range(9)
        )
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=11)
        candidates = {item.ticker: item for item in report.candidates}
        nometa = candidates["NOMETA"]
        fullhigh = candidates["FULLHIGH"]
        fullmid = candidates["FULLMID"]
        self.assertEqual(report.sector_aware_status, "enabled")
        self.assertEqual(nometa.official_score_source, "legacy_missing_metadata")
        self.assertEqual(nometa.total_score, nometa.legacy_total_score)
        self.assertEqual(nometa.sector_relative_peer_source, "universe_missing_metadata")
        self.assertIsNotNone(nometa.sector_relative_score_preview)
        self.assertGreater(nometa.sector_relative_score_preview or 0, nometa.legacy_total_score or 0)
        self.assertEqual(nometa.suggested_action, screener.assign_hybrid_action(nometa.record, nometa.legacy_total_score, nometa.legacy_risk_safety_score)[0])
        ranked_tickers = [item.ticker for item in report.candidates]
        self.assertLess(ranked_tickers.index("FULLHIGH"), ranked_tickers.index("NOMETA"))
        self.assertLess(ranked_tickers.index("FULLMID"), ranked_tickers.index("NOMETA"))
        self.assertGreater(nometa.sector_relative_score_preview or 0, fullmid.total_score or 0)
        self.assertEqual(fullhigh.official_score_source, "sector_aware")

    def test_shadow_preview_missing_metadata_uses_universe_missing_metadata_source(self) -> None:
        candidates = [
            screener.ScreenResult(
                ticker="NOMETA",
                strategy_mode="hybrid",
                total_score=50,
                raw_score=50,
                adjusted_score=50,
                fundamental_score=50,
                momentum_score=50,
                risk_safety_score=50,
                factor_scores={},
                reasons=[],
                risk_warnings=[],
                confidence_notes=[],
                record=screener.StockRecord(
                    ticker="NOMETA",
                    price=100,
                    market_cap=50_000_000_000,
                    avg_dollar_volume_20d=100_000_000,
                    revenue_growth_yoy=10,
                    eps_growth_yoy=10,
                    gross_margin=45,
                    operating_margin=20,
                    return_on_equity=15,
                    pe_ratio=25,
                    ps_ratio=6,
                    relative_strength_252d=70,
                    price_vs_sma200_pct=6,
                    beta=1.0,
                    volatility_63d=25,
                    max_drawdown_252d=15,
                ),
            )
        ]
        screener.apply_sector_relative_preview(candidates, "hybrid")
        self.assertEqual(candidates[0].sector_relative_peer_source, "universe_missing_metadata")

    def test_winsorize_value_clips_bounds(self) -> None:
        self.assertEqual(screener.winsorize_value(120, 0, 100), 100)
        self.assertEqual(screener.winsorize_value(-5, 0, 100), 0)
        self.assertEqual(screener.winsorize_value(50, 0, 100), 50)

    def test_winsorize_series_clips_extreme_values(self) -> None:
        result = screener.winsorize_series([1, 2, 3, 100], lower_pct=0.25, upper_pct=0.75)
        self.assertEqual(result[0], 1.75)
        self.assertEqual(result[1], 2)
        self.assertEqual(result[2], 3)
        self.assertEqual(result[3], 27.25)

    def test_winsorize_series_ignores_none(self) -> None:
        result = screener.winsorize_series([None, 1, 2, 100, float("nan")], lower_pct=0.0, upper_pct=0.5)
        self.assertIsNone(result[0])
        self.assertEqual(result[1], 1)
        self.assertEqual(result[2], 2)
        self.assertEqual(result[3], 2)
        self.assertIsNone(result[4])

    def test_percentile_rank_basic(self) -> None:
        values = [10, 20, 30]
        self.assertEqual(screener.percentile_rank(10, values), 0)
        self.assertEqual(screener.percentile_rank(20, values), 50)
        self.assertEqual(screener.percentile_rank(30, values), 100)

    def test_percentile_rank_handles_ties(self) -> None:
        self.assertEqual(screener.percentile_rank(20, [10, 20, 20, 30]), 50)

    def test_percentile_rank_interpolates_between_values(self) -> None:
        self.assertEqual(screener.percentile_rank(25, [10, 20, 30]), 75)

    def test_percentile_rank_empty_values(self) -> None:
        self.assertIsNone(screener.percentile_rank(10, []))

    def test_percentile_rank_single_value(self) -> None:
        self.assertEqual(screener.percentile_rank(10, [10]), 50)

    def test_percentile_rank_with_nan(self) -> None:
        self.assertIsNone(screener.percentile_rank(float("nan"), [10, 20, 30]))
        self.assertEqual(screener.percentile_rank(20, [10, float("nan"), 20, None, 30]), 50)

    def test_score_higher_is_better(self) -> None:
        self.assertEqual(screener.score_higher_is_better(30, [10, 20, 30]), 100)
        self.assertEqual(screener.score_higher_is_better(10, [10, 20, 30]), 0)

    def test_score_lower_is_better(self) -> None:
        self.assertEqual(screener.score_lower_is_better(10, [10, 20, 30]), 100)
        self.assertEqual(screener.score_lower_is_better(30, [10, 20, 30]), 0)

    def test_score_missing_policy_neutral(self) -> None:
        self.assertEqual(screener.score_higher_is_better(None, [10, 20, 30], missing_policy="neutral"), 50)

    def test_score_missing_policy_ignore(self) -> None:
        self.assertIsNone(screener.score_higher_is_better(None, [10, 20, 30], missing_policy="ignore"))

    def test_score_missing_policy_penalize(self) -> None:
        self.assertEqual(
            screener.score_with_missing_policy(None, [10, 20, 30], "higher_is_better", "penalize", penalize_score=15),
            15,
        )

    def test_score_missing_policy_zero(self) -> None:
        self.assertEqual(screener.score_higher_is_better(None, [10, 20, 30], missing_policy="zero"), 0)

    def test_safe_zscore_handles_zero_std(self) -> None:
        self.assertEqual(screener.safe_zscore(10, [10, 10, 10]), 0)

    def test_safe_zscore_basic(self) -> None:
        self.assertAlmostEqual(screener.safe_zscore(30, [10, 20, 30]), 1.224744871, places=6)

    def test_sample_csv_loads(self) -> None:
        sample = SCRIPT_DIR.parent / "references" / "sample-universe.csv"
        records = load_records(sample)
        self.assertEqual(len(records), 4)
        self.assertEqual(records[0].ticker, "AAPL")

    def test_sector_relative_uses_sector_when_enough_peers(self) -> None:
        records = [
            self._sector_preview_record(
                f"TECH{index:02d}",
                revenue_growth_yoy=index,
                eps_growth_yoy=index,
                gross_margin=30 + index,
                operating_margin=10 + index,
                return_on_equity=5 + index,
            )
            for index in range(screener.SECTOR_RELATIVE_MIN_PEERS)
        ]
        records.extend(
            [
                self._sector_preview_record(
                    f"ENER{index:02d}",
                    sector="Energy",
                    revenue_growth_yoy=100,
                    eps_growth_yoy=100,
                )
                for index in range(5)
            ]
        )
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=40)
        target = next(item for item in report.candidates if item.ticker == "TECH29")
        self.assertIsNotNone(target.sector_relative_score_preview)
        self.assertFalse(any("used universe fallback" in note for note in target.sector_relative_notes))

    def test_sector_relative_falls_back_to_universe_when_sector_too_small(self) -> None:
        records = [
            self._sector_preview_record("SMALL1", sector="Industrials"),
            self._sector_preview_record("SMALL2", sector="Industrials", revenue_growth_yoy=20),
        ]
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=2)
        notes = "；".join(report.candidates[0].sector_relative_notes)
        self.assertIn("used universe fallback", notes)

    def test_sector_preview_reports_sector_peer_source(self) -> None:
        records = [
            self._sector_preview_record(
                f"TECH{index:02d}",
                industry="IndustryA" if index < 15 else "IndustryB",
                revenue_growth_yoy=index,
                eps_growth_yoy=index,
            )
            for index in range(screener.SECTOR_RELATIVE_MIN_PEERS)
        ]
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=40)
        target = next(item for item in report.candidates if item.ticker == "TECH29")
        self.assertEqual(target.sector_relative_peer_source, "sector")
        self.assertEqual(target.sector_relative_peer_count, screener.SECTOR_RELATIVE_MIN_PEERS)
        self.assertEqual(screener._format_sector_relative_peer_source(target), "Technology sector")

    def test_sector_preview_reports_universe_fallback_source(self) -> None:
        records = [
            self._sector_preview_record("SMALL1", sector="Industrials"),
            self._sector_preview_record("SMALL2", sector="Industrials", revenue_growth_yoy=20),
            self._sector_preview_record("SMALL3", sector="Energy", revenue_growth_yoy=30),
        ]
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=3)
        target = next(item for item in report.candidates if item.ticker == "SMALL1")
        self.assertEqual(target.sector_relative_peer_source, "universe_insufficient_peers")
        self.assertEqual(screener._format_sector_relative_peer_source(target), "Universe fallback (insufficient peers)")

    def test_sector_preview_reports_peer_count(self) -> None:
        records = [
            self._sector_preview_record("SMALL1", sector="Industrials"),
            self._sector_preview_record("SMALL2", sector="Industrials"),
            self._sector_preview_record("TECH1", sector="Technology"),
            self._sector_preview_record("ENER1", sector="Energy"),
        ]
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=4)
        target = next(item for item in report.candidates if item.ticker == "SMALL1")
        self.assertEqual(target.sector_relative_peer_source, "universe_insufficient_peers")
        self.assertEqual(target.sector_relative_peer_count, 4)

    def test_sector_summary_counts_peer_sources(self) -> None:
        records = [
            self._sector_preview_record(
                f"TECH{index:02d}",
                sector="Technology",
                industry="IndustryA" if index < 15 else "IndustryB",
            )
            for index in range(screener.SECTOR_RELATIVE_MIN_PEERS)
        ]
        records.extend(
            [
                self._sector_preview_record("SMALL1", sector="Industrials"),
                self._sector_preview_record("SMALL2", sector="Industrials"),
                self._sector_preview_record("NOSECTOR", sector=""),
            ]
        )
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=40)
        self.assertEqual(report.sector_aware_sector_peer_used_count, screener.SECTOR_RELATIVE_MIN_PEERS)
        self.assertEqual(report.sector_aware_universe_fallback_count, 2)
        self.assertEqual(report.sector_aware_universe_missing_metadata_count, 1)
        self.assertIsNotNone(report.sector_aware_average_peer_count)
        self.assertEqual(report.sector_aware_min_peer_count, screener.SECTOR_RELATIVE_MIN_PEERS)
        self.assertEqual(report.sector_aware_max_peer_count, len(records))

    def test_sector_relative_higher_is_better_factor(self) -> None:
        records = [
            self._sector_preview_record("LOWG", revenue_growth_yoy=1, eps_growth_yoy=1),
            self._sector_preview_record("HIGHG", revenue_growth_yoy=30, eps_growth_yoy=30),
        ]
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=2)
        by_ticker = {item.ticker: item for item in report.candidates}
        self.assertGreater(
            by_ticker["HIGHG"].sector_relative_factor_scores["growth"] or 0,
            by_ticker["LOWG"].sector_relative_factor_scores["growth"] or 0,
        )

    def test_sector_relative_lower_is_better_factor(self) -> None:
        records = [
            self._sector_preview_record("CHEAP", pe_ratio=10, ps_ratio=2),
            self._sector_preview_record("EXPENSIVE", pe_ratio=80, ps_ratio=20),
        ]
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=2)
        by_ticker = {item.ticker: item for item in report.candidates}
        self.assertGreater(
            by_ticker["CHEAP"].sector_relative_factor_scores["valuation"] or 0,
            by_ticker["EXPENSIVE"].sector_relative_factor_scores["valuation"] or 0,
        )

    def test_sector_relative_missing_factor_ignored(self) -> None:
        records = [
            self._sector_preview_record("PARTIAL", pe_ratio=None, ps_ratio=4),
            self._sector_preview_record("FULL", pe_ratio=20, ps_ratio=8),
        ]
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=2)
        partial = next(item for item in report.candidates if item.ticker == "PARTIAL")
        self.assertIsNotNone(partial.sector_relative_score_preview)
        self.assertIsNotNone(partial.sector_relative_factor_scores["valuation"])
        self.assertTrue(any("pe_ratio" in note for note in partial.sector_relative_notes))

    def test_sector_relative_official_score_preserves_legacy_total_score(self) -> None:
        sample = SCRIPT_DIR.parent / "references" / "sample-universe.csv"
        report = build_report(load_records(sample), self.config, strategy_mode="hybrid", top_n=4)
        legacy_scores = {item.ticker: item.legacy_total_score for item in report.candidates}
        self.assertEqual(legacy_scores["NVDA"], 76.5)
        self.assertEqual(legacy_scores["MSFT"], 74.7)
        self.assertEqual(legacy_scores["AAPL"], 66.5)
        self.assertEqual({item.ticker: item.total_score for item in report.candidates}, legacy_scores)

    def test_sector_relative_official_score_can_change_ranking(self) -> None:
        records = [
            self._sector_preview_record("LOW", revenue_growth_yoy=1, eps_growth_yoy=1, relative_strength_252d=10),
            self._sector_preview_record("HIGH", revenue_growth_yoy=30, eps_growth_yoy=30, relative_strength_252d=90),
        ]
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=2)
        self.assertTrue(report.sector_aware_official_scoring)
        self.assertTrue(any(item.total_score != item.legacy_total_score for item in report.candidates))
        self.assertTrue(all(item.factor_scores.get("sector_aware_official_score") for item in report.candidates))

    def test_sector_relative_official_score_recalculates_suggested_action(self) -> None:
        records = [
            self._sector_preview_record("RISKY", beta=2.5, volatility_63d=80, max_drawdown_252d=50),
            self._sector_preview_record("STEADY", beta=0.8, volatility_63d=15, max_drawdown_252d=8),
        ]
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=2)
        self.assertIn("CANDIDATE_HIGH_RISK", {item.suggested_action for item in report.candidates})

    def test_sector_relative_rank_delta(self) -> None:
        weak = screener.ScreenResult(
            ticker="WEAK",
            strategy_mode="hybrid",
            total_score=90,
            raw_score=90,
            adjusted_score=90,
            fundamental_score=90,
            momentum_score=90,
            risk_safety_score=90,
            factor_scores={},
            reasons=[],
            risk_warnings=[],
            confidence_notes=[],
            record=screener.StockRecord(
                ticker="WEAK",
                sector="Technology",
                revenue_growth_yoy=1,
                eps_growth_yoy=1,
                gross_margin=20,
                operating_margin=5,
                return_on_equity=3,
                pe_ratio=80,
                ps_ratio=20,
                relative_strength_252d=20,
                price_vs_sma200_pct=-10,
                volatility_63d=80,
                beta=2,
                max_drawdown_252d=50,
                avg_dollar_volume_20d=30_000_000,
            ),
        )
        strong = screener.ScreenResult(
            ticker="STRONG",
            strategy_mode="hybrid",
            total_score=80,
            raw_score=80,
            adjusted_score=80,
            fundamental_score=80,
            momentum_score=80,
            risk_safety_score=80,
            factor_scores={},
            reasons=[],
            risk_warnings=[],
            confidence_notes=[],
            record=screener.StockRecord(
                ticker="STRONG",
                sector="Technology",
                revenue_growth_yoy=30,
                eps_growth_yoy=30,
                gross_margin=60,
                operating_margin=35,
                return_on_equity=25,
                pe_ratio=12,
                ps_ratio=3,
                relative_strength_252d=90,
                price_vs_sma200_pct=20,
                volatility_63d=15,
                beta=0.8,
                max_drawdown_252d=8,
                avg_dollar_volume_20d=300_000_000,
            ),
        )
        screener.apply_sector_relative_preview([weak, strong], "hybrid")
        self.assertEqual(strong.sector_relative_rank_preview, 1)
        self.assertEqual(strong.sector_relative_rank_delta, 1)
        self.assertEqual(weak.sector_relative_rank_delta, -1)

    def test_sector_relative_score_delta(self) -> None:
        records = [
            self._sector_preview_record("LOW", revenue_growth_yoy=1, eps_growth_yoy=1),
            self._sector_preview_record("HIGH", revenue_growth_yoy=30, eps_growth_yoy=30),
        ]
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=2)
        candidate = report.candidates[0]
        self.assertIsNotNone(candidate.sector_relative_score_delta)
        self.assertAlmostEqual(
            candidate.sector_relative_score_delta or 0,
            round((candidate.sector_relative_score_preview or 0) - (candidate.legacy_total_score or 0), 1),
        )

    def test_sector_relative_report_summary_and_web_payload(self) -> None:
        payload = {
            "source_mode": "sample_universe",
            "strategy_mode": "hybrid",
            "force_rebalance": False,
            "auto_fetch": False,
        }
        report = run_screen_request(payload)
        self.assertTrue(report["sector_aware_shadow_mode"])
        self.assertFalse(report["sector_aware_official_scoring"])
        self.assertEqual(report["sector_aware_status"], "disabled_insufficient_sector_metadata")
        self.assertIn("sector_aware_preview_available_count", report)
        self.assertIn("sector_metadata_coverage", report)
        self.assertIn("sector_aware_sector_peer_used_count", report)
        self.assertIn("sector_aware_universe_fallback_count", report)
        self.assertIn("sector_aware_universe_missing_metadata_count", report)
        self.assertIn("sector_aware_not_scored_disabled_count", report)
        self.assertIn("market_regime", report)
        self.assertIn("market_regime_status", report)
        self.assertIn("configured_composite_weights", report)
        self.assertIn("effective_composite_weights", report)
        self.assertIn("market_context", report)
        self.assertIn("sector_relative_score_preview", report["candidates"][0])
        self.assertIn("sector_relative_factor_scores", report["candidates"][0])
        self.assertIn("sector_relative_peer_source", report["candidates"][0])
        self.assertIn("sector_relative_peer_count", report["candidates"][0])
        self.assertIn("sector_relative_peer_reason", report["candidates"][0])
        self.assertIn("official_score_source", report["candidates"][0])
        self.assertIn("base_total_score", report["candidates"][0])
        self.assertIn("market_regime_score_delta", report["candidates"][0])

    def test_sector_relative_preview_coverage_and_correlation(self) -> None:
        records = [
            self._sector_preview_record("LOW", revenue_growth_yoy=1, eps_growth_yoy=1),
            self._sector_preview_record("MID", revenue_growth_yoy=10, eps_growth_yoy=10),
            self._sector_preview_record("HIGH", revenue_growth_yoy=30, eps_growth_yoy=30),
        ]
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=3)
        self.assertEqual(report.sector_aware_preview_coverage, 1.0)
        self.assertIsNotNone(report.sector_aware_score_correlation_with_current)
        self.assertGreaterEqual(report.sector_aware_score_correlation_with_current or 0, -1.0)
        self.assertLessEqual(report.sector_aware_score_correlation_with_current or 0, 1.0)

    def test_sector_relative_top_10_overlap(self) -> None:
        records = [
            self._sector_preview_record(f"OVL{index:02d}", revenue_growth_yoy=index, eps_growth_yoy=index)
            for index in range(12)
        ]
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=12)
        self.assertIsNotNone(report.sector_aware_top_10_overlap)
        self.assertGreaterEqual(report.sector_aware_top_10_overlap or 0, 0)
        self.assertLessEqual(report.sector_aware_top_10_overlap or 0, report.sector_aware_top_10_overlap_total)
        self.assertEqual(report.sector_aware_top_10_overlap_total, 10)

    def test_top_10_overlap_formats_as_n_over_total(self) -> None:
        records = [
            self._sector_preview_record(f"OVL{index:02d}", revenue_growth_yoy=index, eps_growth_yoy=index)
            for index in range(3)
        ]
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=3)
        markdown = screener._render_markdown(report)
        self.assertIn(
            f"sector_aware_top_10_overlap：{report.sector_aware_top_10_overlap} / {report.sector_aware_top_10_overlap_total}",
            markdown,
        )

    def test_sector_relative_large_rank_change_summary(self) -> None:
        candidates = []
        for index in range(25):
            candidates.append(
                screener.ScreenResult(
                    ticker=f"REV{index:02d}",
                    strategy_mode="hybrid",
                    total_score=100 - index,
                    raw_score=100 - index,
                    adjusted_score=100 - index,
                    fundamental_score=100 - index,
                    momentum_score=100 - index,
                    risk_safety_score=100 - index,
                    factor_scores={},
                    reasons=[],
                    risk_warnings=[],
                    confidence_notes=[],
                    record=screener.StockRecord(
                        ticker=f"REV{index:02d}",
                        sector="Technology",
                        revenue_growth_yoy=index,
                        eps_growth_yoy=index,
                        gross_margin=20 + index,
                        operating_margin=5 + index,
                        return_on_equity=5 + index,
                        pe_ratio=80 - index,
                        ps_ratio=30 - index,
                        relative_strength_252d=index,
                        price_vs_sma200_pct=index,
                        volatility_63d=80 - index,
                        beta=2.5 - index * 0.05,
                        max_drawdown_252d=50 - index,
                        avg_dollar_volume_20d=30_000_000 + index * 1_000_000,
                    ),
                )
            )
        summary = screener.apply_sector_relative_preview(candidates, "hybrid")
        self.assertEqual(summary["sector_aware_top_10_overlap"], 0)
        self.assertGreater(summary["sector_aware_large_rank_change_count"], 0)
        self.assertEqual(summary["sector_aware_large_rank_change_threshold"], screener.SECTOR_RELATIVE_LARGE_RANK_CHANGE_THRESHOLD)
        self.assertTrue(summary["sector_aware_largest_movers"])

    def test_sector_relative_largest_movers_include_factor_explanation(self) -> None:
        candidates = []
        for index in range(3):
            candidates.append(
                screener.ScreenResult(
                    ticker=f"MOV{index}",
                    strategy_mode="hybrid",
                    total_score=90 - index,
                    raw_score=90 - index,
                    adjusted_score=90 - index,
                    fundamental_score=90 - index,
                    momentum_score=90 - index,
                    risk_safety_score=90 - index,
                    factor_scores={},
                    reasons=[],
                    risk_warnings=[],
                    confidence_notes=[],
                    record=screener.StockRecord(
                        ticker=f"MOV{index}",
                        sector="Technology",
                        revenue_growth_yoy=index,
                        eps_growth_yoy=index,
                        gross_margin=20 + index,
                        operating_margin=5 + index,
                        return_on_equity=5 + index,
                        pe_ratio=80 - index,
                        ps_ratio=30 - index,
                        relative_strength_252d=index,
                        price_vs_sma200_pct=index,
                        volatility_63d=80 - index,
                        beta=2.5 - index * 0.05,
                        max_drawdown_252d=50 - index,
                        avg_dollar_volume_20d=30_000_000 + index * 1_000_000,
                    ),
                )
            )
        summary = screener.apply_sector_relative_preview(candidates, "hybrid")
        mover = summary["sector_aware_largest_movers"][0]
        self.assertIn("factor_scores", mover)
        self.assertIn("growth", mover["factor_scores"])
        self.assertIn("notes", mover)

    def test_sector_relative_diagnostics_do_not_change_sample_ranking(self) -> None:
        sample = SCRIPT_DIR.parent / "references" / "sample-universe.csv"
        report = build_report(load_records(sample), self.config, strategy_mode="hybrid", top_n=4)
        self.assertEqual([item.ticker for item in report.candidates], ["NVDA", "MSFT", "AAPL"])
        self.assertEqual([item.legacy_total_score for item in report.candidates], [76.5, 74.7, 66.5])
        self.assertEqual([item.total_score for item in report.candidates], [76.5, 74.7, 66.5])
        self.assertEqual(report.sector_aware_status, "disabled_insufficient_sector_metadata")

    def test_sector_aware_official_marks_factor_scores(self) -> None:
        records = [
            self._sector_preview_record("AAA", revenue_growth_yoy=1, eps_growth_yoy=1),
            self._sector_preview_record("BBB", revenue_growth_yoy=30, eps_growth_yoy=30),
        ]
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=2)
        self.assertTrue(report.sector_aware_official_scoring)
        self.assertTrue(all(item.factor_scores.get("sector_aware_official_score") for item in report.candidates))

    def test_sector_aware_official_preserves_legacy_factor_scores(self) -> None:
        sample = SCRIPT_DIR.parent / "references" / "sample-universe.csv"
        report = build_report(load_records(sample), self.config, strategy_mode="hybrid", top_n=4)
        candidate = report.candidates[0]
        self.assertIsNotNone(candidate.legacy_fundamental_score)
        self.assertIsNotNone(candidate.legacy_momentum_score)
        self.assertIsNotNone(candidate.legacy_risk_safety_score)
        self.assertIn("legacy_fundamental_score", candidate.factor_scores)

    def test_sector_aware_official_uses_config_weights(self) -> None:
        records = [
            self._sector_preview_record("QUALITY", revenue_growth_yoy=30, eps_growth_yoy=30, relative_strength_252d=10),
            self._sector_preview_record("MOMENTUM", revenue_growth_yoy=1, eps_growth_yoy=1, relative_strength_252d=100),
        ]
        quality_config = ScreenConfig(fundamental_weight=0.80, momentum_weight=0.10, risk_weight=0.10)
        momentum_config = ScreenConfig(fundamental_weight=0.10, momentum_weight=0.80, risk_weight=0.10)
        quality_report = build_report(records, quality_config, strategy_mode="hybrid", top_n=2)
        momentum_report = build_report(records, momentum_config, strategy_mode="hybrid", top_n=2)
        self.assertNotEqual(quality_report.candidates[0].ticker, momentum_report.candidates[0].ticker)

    def test_stop_sector_aware_official_preserves_legacy_score(self) -> None:
        records = [
            self._sector_preview_record("STOPA", free_cash_flow=2_000_000_000, shares_growth_yoy=0.01, roe=0.20, roic=0.14),
            self._sector_preview_record("STOPB", free_cash_flow=1_000_000_000, shares_growth_yoy=0.02, roe=0.12, roic=0.08),
        ]
        report = build_report(records, self.config, strategy_mode="stop_checking_price", min_score=0, top_n=2)
        self.assertTrue(all(item.legacy_total_score is not None for item in report.candidates))
        self.assertTrue(all(item.factor_scores.get("sector_aware_official_score") for item in report.candidates))

    def test_stop_sector_aware_raw_score_uses_preview_score(self) -> None:
        records = [
            self._sector_preview_record("STOPA", free_cash_flow=2_000_000_000, shares_growth_yoy=0.01, roe=0.20, roic=0.14),
            self._sector_preview_record("STOPB", free_cash_flow=1_000_000_000, shares_growth_yoy=0.02, roe=0.12, roic=0.08),
        ]
        report = build_report(records, self.config, strategy_mode="stop_checking_price", min_score=0, top_n=2)
        candidate = report.candidates[0]
        self.assertEqual(candidate.raw_score, candidate.sector_relative_score_preview)

    def test_sector_aware_score_delta_compares_against_legacy_score(self) -> None:
        records = [
            self._sector_preview_record("LOW", revenue_growth_yoy=1, eps_growth_yoy=1),
            self._sector_preview_record("HIGH", revenue_growth_yoy=30, eps_growth_yoy=30),
        ]
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=2)
        candidate = report.candidates[0]
        self.assertAlmostEqual(
            candidate.sector_relative_score_delta or 0,
            round((candidate.sector_relative_score_preview or 0) - (candidate.legacy_total_score or 0), 1),
        )

    def test_industry_peer_source_takes_precedence_over_sector(self) -> None:
        records = [
            self._sector_preview_record(f"SW{index:02d}", sector="Technology", industry="Software")
            for index in range(screener.SECTOR_RELATIVE_MIN_PEERS)
        ]
        records.extend(
            [
                self._sector_preview_record(f"HW{index:02d}", sector="Technology", industry="Hardware")
                for index in range(screener.SECTOR_RELATIVE_MIN_PEERS)
            ]
        )
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=80)
        target = next(item for item in report.candidates if item.ticker == "SW29")
        self.assertEqual(target.sector_relative_peer_source, "industry")
        self.assertEqual(target.sector_relative_peer_count, screener.SECTOR_RELATIVE_MIN_PEERS)

    def test_sector_peer_source_used_when_industry_insufficient(self) -> None:
        records = [
            self._sector_preview_record(f"SW{index:02d}", sector="Technology", industry="Software")
            for index in range(5)
        ]
        records.extend(
            [
                self._sector_preview_record(f"HW{index:02d}", sector="Technology", industry="Hardware")
                for index in range(screener.SECTOR_RELATIVE_MIN_PEERS)
            ]
        )
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=40)
        target = next(item for item in report.candidates if item.ticker == "SW00")
        self.assertEqual(target.sector_relative_peer_source, "sector")
        self.assertEqual(target.sector_relative_peer_count, len(records))

    def test_cli_gui_web_show_same_strategy_status_and_peer_provenance(self) -> None:
        sample = SCRIPT_DIR.parent / "references" / "sample-universe.csv"
        report = build_report(load_records(sample), self.config, strategy_mode="hybrid", top_n=4)
        markdown = screener._render_markdown(report)
        gui_text = gui_report_to_text(report, "test")
        web_payload = run_screen_request(
            {
                "source_mode": "sample_universe",
                "strategy_mode": "hybrid",
                "force_rebalance": False,
                "auto_fetch": False,
            }
        )
        self.assertIn("sector_aware_status：disabled_insufficient_sector_metadata", markdown)
        self.assertIn("market_regime：neutral", markdown)
        self.assertIn("sector_aware_status：disabled_insufficient_sector_metadata", gui_text)
        self.assertIn("market_regime：neutral", gui_text)
        self.assertEqual(web_payload["sector_aware_status"], "disabled_insufficient_sector_metadata")
        self.assertEqual(web_payload["market_regime"], "neutral")
        self.assertEqual(web_payload["market_regime_status"], "insufficient_market_data")
        self.assertIn("Official source", markdown)
        self.assertIn("Official source", gui_text)
        self.assertIn("official_score_source", web_payload["candidates"][0])
        self.assertIn("base_total_score", web_payload["candidates"][0])
        self.assertIn("market_regime_score_delta", web_payload["candidates"][0])
        self.assertIn("Peer reason", markdown)
        self.assertIn("Peer reason", gui_text)
        self.assertIn("sector_relative_peer_reason", web_payload["candidates"][0])

    def test_sector_aware_winsorization_limits_outlier_effect(self) -> None:
        records = [
            self._sector_preview_record(f"NORM{index:02d}", revenue_growth_yoy=10 + index, eps_growth_yoy=10 + index)
            for index in range(20)
        ]
        records.append(self._sector_preview_record("OUTLIER", revenue_growth_yoy=10000, eps_growth_yoy=10000))
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=25)
        outlier = next(item for item in report.candidates if item.ticker == "OUTLIER")
        self.assertLessEqual(outlier.sector_relative_factor_scores["growth"] or 0, 100)

    def test_web_payload_includes_legacy_score_fields(self) -> None:
        payload = {
            "source_mode": "sample_universe",
            "strategy_mode": "hybrid",
            "force_rebalance": False,
            "auto_fetch": False,
        }
        report = run_screen_request(payload)
        candidate = report["candidates"][0]
        self.assertIn("legacy_total_score", candidate)
        self.assertIn("legacy_fundamental_score", candidate)

    def test_markdown_includes_legacy_score_column(self) -> None:
        sample = SCRIPT_DIR.parent / "references" / "sample-universe.csv"
        report = build_report(load_records(sample), self.config, strategy_mode="hybrid", top_n=4)
        markdown = screener._render_markdown(report)
        self.assertIn("Legacy score", markdown)

    def test_min_score_filters_on_sector_aware_official_score(self) -> None:
        sample = SCRIPT_DIR.parent / "references" / "sample-universe.csv"
        report = build_report(load_records(sample), self.config, strategy_mode="hybrid", top_n=4, min_score=60)
        self.assertEqual([item.ticker for item in report.candidates], ["NVDA", "MSFT", "AAPL"])

    def test_sector_aware_official_score_changes_at_least_one_sample_score(self) -> None:
        records = [
            self._sector_preview_record("LOW", revenue_growth_yoy=1, eps_growth_yoy=1),
            self._sector_preview_record("HIGH", revenue_growth_yoy=30, eps_growth_yoy=30),
        ]
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=2)
        self.assertTrue(any(item.total_score != item.legacy_total_score for item in report.candidates))

    def test_sample_watchlist_loads(self) -> None:
        sample = SCRIPT_DIR.parent / "references" / "sample-watchlist.csv"
        tickers = load_watchlist(sample)
        self.assertEqual(tickers, ["AAPL", "MSFT", "NVDA", "LOWV"])

    def test_ranking_and_exclusion(self) -> None:
        records = [
            {
                "ticker": "AAA",
                "price": 180,
                "market_cap": 450000000000,
                "avg_dollar_volume_20d": 1200000000,
                "revenue_growth_yoy": 18,
                "eps_growth_yoy": 22,
                "gross_margin": 44,
                "operating_margin": 30,
                "return_on_equity": 24,
                "pe_ratio": 28,
                "ps_ratio": 8,
                "relative_strength_252d": 88,
                "price_vs_sma50_pct": 8,
                "price_vs_sma200_pct": 16,
                "beta": 1.05,
                "volatility_63d": 22,
                "max_drawdown_252d": 12,
                "debt_to_equity": 0.8,
            },
            {
                "ticker": "BBB",
                "price": 14,
                "market_cap": 7000000000,
                "avg_dollar_volume_20d": 50000000,
                "revenue_growth_yoy": 4,
                "eps_growth_yoy": 3,
                "gross_margin": 18,
                "operating_margin": 7,
                "return_on_equity": 6,
                "pe_ratio": 52,
                "ps_ratio": 5,
                "relative_strength_252d": 43,
                "price_vs_sma50_pct": -2,
                "price_vs_sma200_pct": -8,
                "beta": 1.8,
                "volatility_63d": 40,
                "max_drawdown_252d": 32,
                "debt_to_equity": 2.4,
            },
            {
                "ticker": "PENNY",
                "price": 2.1,
                "market_cap": 180000000,
                "avg_dollar_volume_20d": 1200000,
                "is_otc": True,
            },
            {
                "ticker": "HALT",
                "price": 30,
                "market_cap": 5000000000,
                "avg_dollar_volume_20d": 45000000,
                "halted": True,
            },
        ]
        report = build_report(records, self.config)
        tickers = [item.ticker for item in report.candidates]
        self.assertEqual(tickers[0], "AAA")
        self.assertIn("BBB", tickers)
        self.assertEqual(len(report.excluded), 2)
        excluded_reasons = {item.ticker: item.excluded_reason for item in report.excluded}
        self.assertEqual(excluded_reasons["PENNY"], "OTC 標的，直接排除")
        self.assertEqual(excluded_reasons["HALT"], "停牌，今天不納入候選")
        self.assertGreater(report.candidates[0].total_score, report.candidates[1].total_score)

    def test_missing_fundamentals_do_not_crash(self) -> None:
        records = [
            {
                "ticker": "MISS",
                "price": 42,
                "market_cap": 12000000000,
                "avg_volume_20d": 2500000,
                "revenue_growth_yoy": None,
                "eps_growth_yoy": None,
                "gross_margin": None,
                "operating_margin": None,
                "return_on_equity": None,
                "pe_ratio": None,
                "ps_ratio": 4,
                "relative_strength_252d": 61,
                "price_vs_sma50_pct": 3,
                "price_vs_sma200_pct": 6,
                "beta": 1.2,
                "volatility_63d": 24,
                "max_drawdown_252d": 18,
                "data_age_days": 2,
            }
        ]
        report = build_report(records, self.config)
        self.assertEqual(len(report.candidates), 1)
        candidate = report.candidates[0]
        self.assertEqual(candidate.ticker, "MISS")
        self.assertIsNotNone(candidate.total_score)
        self.assertTrue(candidate.confidence_notes)
        self.assertIn("缺少 revenue_growth_yoy, eps_growth_yoy", " ".join(candidate.confidence_notes))

    def test_avg_volume_alias_is_accepted(self) -> None:
        records = [
            {
                "ticker": "ALIAS",
                "price": 50,
                "market_cap": 30000000000,
                "avg_volume_20d": 500000,
                "revenue_growth_yoy": 10,
                "eps_growth_yoy": 12,
                "gross_margin": 35,
                "operating_margin": 20,
                "return_on_equity": 15,
                "pe_ratio": 24,
                "ps_ratio": 6,
                "relative_strength_252d": 70,
                "price_vs_sma50_pct": 4,
                "price_vs_sma200_pct": 8,
                "beta": 1.0,
                "volatility_63d": 25,
                "max_drawdown_252d": 15,
                "data_age_days": 7,
            }
        ]
        report = build_report(records, self.config)
        self.assertEqual(len(report.candidates), 1)
        self.assertEqual(report.candidates[0].ticker, "ALIAS")
        self.assertTrue(any("資料已 7 天未更新" in note for note in report.candidates[0].confidence_notes))

    def test_hard_filter_is_explicit(self) -> None:
        records = [
            {
                "ticker": "SMALL",
                "price": 20,
                "market_cap": 1800000000,
                "avg_dollar_volume_20d": 30000000,
            },
            {
                "ticker": "THIN",
                "price": 20,
                "market_cap": 5000000000,
                "avg_dollar_volume_20d": 15000000,
            },
        ]
        report = build_report(records, self.config)
        excluded = {item.ticker: item.excluded_reason for item in report.excluded}
        self.assertIn("市值 18.0 億美元，低於門檻 20.0 億美元", excluded["SMALL"])
        self.assertIn("20日均成交額 1,500 萬美元，低於門檻 2,000 萬美元", excluded["THIN"])

    def test_hard_pass_count_is_separate_from_display_count(self) -> None:
        records = [
            {
                "ticker": f"T{index:02d}",
                "price": 50,
                "market_cap": 30000000000,
                "avg_dollar_volume_20d": 50000000,
                "revenue_growth_yoy": 0.10,
                "eps_growth_yoy": 0.10,
                "gross_margin": 40,
                "operating_margin": 20,
                "return_on_equity": 15,
                "pe_ratio": 24,
                "ps_ratio": 6,
                "relative_strength_252d": 70,
                "price_vs_sma50_pct": 4,
                "price_vs_sma200_pct": 8,
                "beta": 1.0,
                "volatility_63d": 25,
                "max_drawdown_252d": 15,
                "data_age_days": 2,
            }
            for index in range(25)
        ]
        report = build_report(records, self.config)
        self.assertEqual(report.universe_size, 25)
        self.assertEqual(report.hard_pass_count, 25)
        self.assertEqual(len(report.candidates), 20)

    def test_hybrid_defaults_to_dedupe_company_without_min_score(self) -> None:
        base = {
            "price": 150,
            "market_cap": 1_000_000_000_000,
            "avg_dollar_volume_20d": 900_000_000,
            "revenue_growth_yoy": 12,
            "eps_growth_yoy": 15,
            "gross_margin": 45,
            "operating_margin": 26,
            "return_on_equity": 18,
            "pe_ratio": 24,
            "ps_ratio": 6,
            "relative_strength_252d": 75,
            "price_vs_sma50_pct": 4,
            "price_vs_sma200_pct": 8,
            "beta": 1.0,
            "volatility_63d": 22,
            "max_drawdown_252d": 15,
        }
        records = [dict(base, ticker="GOOG"), dict(base, ticker="GOOGL"), dict(base, ticker="MSFT")]
        report = build_report(records, self.config, strategy_mode="hybrid")
        tickers = [item.ticker for item in report.candidates]
        self.assertTrue(report.dedupe_company)
        self.assertEqual(report.dedupe_removed_count, 1)
        self.assertEqual(sum(1 for ticker in tickers if ticker in {"GOOG", "GOOGL"}), 1)
        self.assertIsNone(report.min_score)
        self.assertEqual(report.effective_min_score_source, "none")

    def test_hybrid_ranking_diagnostics_and_action_labels(self) -> None:
        momentum_driven = {
            "ticker": "MOMO1",
            "price": 100,
            "market_cap": 80_000_000_000,
            "avg_dollar_volume_20d": 300_000_000,
            "revenue_growth_yoy": 0,
            "eps_growth_yoy": 0,
            "gross_margin": 35,
            "operating_margin": 22.5,
            "return_on_equity": 15,
            "pe_ratio": 24,
            "ps_ratio": 6,
            "relative_strength_252d": 100,
            "price_vs_sma50_pct": 25,
            "price_vs_sma200_pct": 45,
            "beta": 1.0,
            "volatility_63d": 20,
            "max_drawdown_252d": 12,
            "data_age_days": 2,
        }
        high_risk = dict(
            momentum_driven,
            ticker="RISKY",
            revenue_growth_yoy=25,
            eps_growth_yoy=30,
            gross_margin=55,
            operating_margin=35,
            return_on_equity=25,
            pe_ratio=50,
            ps_ratio=6,
            beta=2.0,
            volatility_63d=55,
            max_drawdown_252d=45,
        )
        data_limited = dict(momentum_driven, ticker="LIMITED", beta=None)
        report = build_report([momentum_driven, high_risk, data_limited], self.config, strategy_mode="hybrid", top_n=3)
        actions = {item.ticker: item.suggested_action for item in report.candidates}
        self.assertEqual(report.ranking_style, "momentum_driven")
        self.assertGreater(report.top_n_average_momentum_score or 0, report.top_n_average_fundamental_score or 0)
        self.assertEqual(actions["RISKY"], "CANDIDATE_HIGH_RISK")
        self.assertEqual(actions["LIMITED"], "CANDIDATE_DATA_LIMITED")
        self.assertEqual(report.high_risk_candidate_count, 1)
        self.assertEqual(report.expensive_candidate_count, 1)
        self.assertEqual(report.high_volatility_candidate_count, 1)
        self.assertEqual(report.deep_drawdown_candidate_count, 1)
        self.assertEqual(report.missing_data_candidate_count, 1)

    def test_min_score_source_is_explicit(self) -> None:
        record = {
            "ticker": "QUAL",
            "price": 120,
            "market_cap": 60_000_000_000,
            "avg_dollar_volume_20d": 180_000_000,
            "revenue_growth_yoy": 0.22,
            "eps_growth_yoy": 0.24,
            "gross_margin_ttm": 0.58,
            "operating_margin_ttm": 0.27,
            "roe": 0.21,
            "roic": 0.17,
            "free_cash_flow": 3_500_000_000,
            "debt_to_equity": 0.6,
            "shares_growth_yoy": 0.01,
            "pe_ratio": 28,
            "ps_ratio": 7,
            "max_drawdown_1y": -0.18,
            "volatility_1y": 0.22,
            "price_vs_200dma": 0.08,
            "data_age_days": 2,
        }
        hybrid_report = build_report([dict(record)], self.config, strategy_mode="hybrid")
        stop_report = build_report([dict(record)], self.config, strategy_mode="stop_checking_price")
        user_report = build_report([dict(record)], self.config, strategy_mode="stop_checking_price", min_score=70)
        self.assertIsNone(hybrid_report.min_score)
        self.assertEqual(hybrid_report.effective_min_score_source, "none")
        self.assertIsNone(stop_report.min_score)
        self.assertEqual(stop_report.effective_min_score_source, "none")
        self.assertEqual(user_report.min_score, 70.0)
        self.assertEqual(user_report.effective_min_score_source, "user")

    def test_bad_json_raises_helpful_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bad.json"
            path.write_text("{not json}", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                load_records(path)
        self.assertIn("Invalid JSON", str(ctx.exception))

    def test_stability_for_same_input(self) -> None:
        records = [
            {
                "ticker": "STB",
                "price": 95,
                "market_cap": 25000000000,
                "avg_dollar_volume_20d": 90000000,
                "revenue_growth_yoy": 12,
                "eps_growth_yoy": 15,
                "gross_margin": 33,
                "operating_margin": 21,
                "return_on_equity": 18,
                "pe_ratio": 24,
                "ps_ratio": 6,
                "relative_strength_252d": 77,
                "price_vs_sma50_pct": 5,
                "price_vs_sma200_pct": 11,
                "beta": 1.1,
                "volatility_63d": 26,
                "max_drawdown_252d": 14,
            }
        ]
        first = build_report(records, self.config)
        second = build_report(records, self.config)
        self.assertEqual(first.candidates[0].total_score, second.candidates[0].total_score)
        self.assertEqual(first.candidates[0].factor_scores, second.candidates[0].factor_scores)

    def test_config_weight_shift_changes_score(self) -> None:
        records = [
            {
                "ticker": "FUND",
                "price": 70,
                "market_cap": 40000000000,
                "avg_dollar_volume_20d": 100000000,
                "revenue_growth_yoy": 24,
                "eps_growth_yoy": 26,
                "gross_margin": 48,
                "operating_margin": 28,
                "return_on_equity": 25,
                "pe_ratio": 30,
                "ps_ratio": 9,
                "relative_strength_252d": 62,
                "price_vs_sma50_pct": 2,
                "price_vs_sma200_pct": 4,
                "beta": 1.0,
                "volatility_63d": 18,
                "max_drawdown_252d": 11,
            },
            {
                "ticker": "MOMO",
                "price": 55,
                "market_cap": 18000000000,
                "avg_dollar_volume_20d": 85000000,
                "revenue_growth_yoy": 8,
                "eps_growth_yoy": 9,
                "gross_margin": 24,
                "operating_margin": 12,
                "return_on_equity": 11,
                "pe_ratio": 22,
                "ps_ratio": 5,
                "relative_strength_252d": 92,
                "price_vs_sma50_pct": 14,
                "price_vs_sma200_pct": 19,
                "beta": 1.3,
                "volatility_63d": 30,
                "max_drawdown_252d": 18,
            },
        ]
        base = build_report(records, self.config)
        momentum_config = ScreenConfig(fundamental_weight=0.20, momentum_weight=0.60, risk_weight=0.20)
        shifted = build_report(records, momentum_config)
        base_rank = [item.ticker for item in base.candidates]
        shifted_rank = [item.ticker for item in shifted.candidates]
        self.assertNotEqual(base_rank, shifted_rank)

    def test_strategy_weight_maps_sum_to_one(self) -> None:
        for mode in screener.VALID_STRATEGY_MODES:
            self.assertAlmostEqual(sum(screener.STRATEGY_WEIGHTS[mode].values()), 1.0)
            self.assertAlmostEqual(sum(screener.FUNDAMENTAL_WEIGHTS[mode].values()), 1.0)
            self.assertAlmostEqual(sum(screener.MOMENTUM_WEIGHTS[mode].values()), 1.0)
            self.assertAlmostEqual(sum(screener.RISK_WEIGHTS[mode].values()), 1.0)

    def test_hybrid_mode_preserves_existing_weights(self) -> None:
        self.assertEqual(screener.STRATEGY_WEIGHTS["hybrid"]["fundamental"], 0.40)
        self.assertEqual(screener.STRATEGY_WEIGHTS["hybrid"]["momentum"], 0.35)
        self.assertEqual(screener.STRATEGY_WEIGHTS["hybrid"]["risk_safety"], 0.25)

    def test_stop_checking_price_weights(self) -> None:
        self.assertEqual(
            screener.STRATEGY_WEIGHTS["stop_checking_price"],
            {"fundamental": 0.55, "risk_safety": 0.30, "momentum": 0.15},
        )

    def test_stop_mode_confidence_and_output_schema(self) -> None:
        records = [
            {
                "ticker": "QUAL",
                "company_name": "Quality Co",
                "sector": "Technology",
                "industry": "Software",
                "price": 120,
                "market_cap": 60000000000,
                "avg_dollar_volume_20d": 180000000,
                "revenue_growth_yoy": 0.22,
                "eps_growth_yoy": 0.24,
                "gross_margin_ttm": 0.58,
                "operating_margin_ttm": 0.27,
                "net_margin_ttm": 0.19,
                "roe": 0.21,
                "roic": 0.17,
                "free_cash_flow": 3_500_000_000,
                "fcf_margin": 0.24,
                "fcf_growth_yoy": 0.20,
                "revenue_growth_3y_cagr": 0.18,
                "debt_to_equity": 0.6,
                "net_debt_to_ebitda": 0.9,
                "current_ratio": 2.1,
                "interest_coverage": 12,
                "shares_growth_yoy": 0.01,
                "shares_growth_3y_cagr": 0.00,
                "pe_ratio": 28,
                "forward_pe": 24,
                "ps_ratio": 7,
                "ev_to_ebitda": 16,
                "peg_ratio": 1.4,
                "relative_strength_252d": 78,
                "price_vs_200dma": 0.08,
                "price_vs_sma50_pct": 5,
                "price_vs_sma200_pct": 9,
                "max_drawdown_1y": -0.18,
                "volatility_1y": 0.22,
                "data_age_days": 2,
            }
        ]
        report = build_report(records, self.config, strategy_mode="stop_checking_price", as_of=date(2026, 4, 20))
        self.assertEqual(report.strategy_mode, "stop_checking_price")
        self.assertEqual(report.review_mode, "watchlist_only")
        self.assertEqual(len(report.candidates), 1)
        candidate = report.candidates[0]
        self.assertEqual(candidate.strategy_mode, "stop_checking_price")
        self.assertIsNotNone(candidate.company_snapshot)
        self.assertAlmostEqual(candidate.confidence_score or 0.0, 1.0)
        self.assertEqual(candidate.confidence_label, "high")
        self.assertIsInstance(candidate.penalties, list)
        self.assertIsNotNone(candidate.suggested_action)
        self.assertLessEqual(screener.STOP_ACTION_RANK[candidate.suggested_action], screener.STOP_ACTION_RANK["WATCHLIST_HIGH_QUALITY"])
        self.assertEqual(candidate.company_snapshot["confidence_label"], "high")
        self.assertIn("company_name", candidate.company_snapshot)
        self.assertIn("debt_to_equity_raw", candidate.company_snapshot)
        self.assertIn("debt_to_equity_normalized", candidate.company_snapshot)
        self.assertIn("data_quality_score", candidate.company_snapshot)
        self.assertIn("data_quality_flags", candidate.company_snapshot)
        self.assertIsNotNone(candidate.penalty_score)
        self.assertIsNotNone(candidate.confidence_multiplier)
        self.assertIsNotNone(candidate.final_score)
        self.assertIsNotNone(candidate.data_quality_score)
        self.assertIsInstance(candidate.data_quality_flags, list)
        self.assertIsNone(candidate.action_cap_reason)

    def test_stop_mode_partial_confidence(self) -> None:
        records = [
            {
                "ticker": "PART",
                "price": 80,
                "market_cap": 30000000000,
                "avg_dollar_volume_20d": 90000000,
                "revenue_growth_yoy": 0.12,
                "eps_growth_yoy": 0.10,
                "gross_margin_ttm": 0.45,
                "operating_margin_ttm": 0.18,
                "roe": 0.15,
                "data_age_days": 2,
            }
        ]
        report = build_report(records, self.config, strategy_mode="stop_checking_price", min_score=0)
        candidate = report.candidates[0]
        self.assertGreaterEqual(candidate.confidence_score or 0.0, 0.45)
        self.assertLessEqual(candidate.confidence_score or 0.0, 0.60)
        self.assertIn(candidate.confidence_label, {"low", "very_low"})

    def test_stop_mode_debt_to_equity_normalization_avoids_bad_hard_exclusion(self) -> None:
        records = [
            {
                "ticker": "AAPL",
                "company_name": "Apple",
                "sector": "Technology",
                "price": 180,
                "market_cap": 2_800_000_000_000,
                "avg_dollar_volume_20d": 120_000_000,
                "revenue_growth_yoy": 0.10,
                "eps_growth_yoy": 0.12,
                "gross_margin_ttm": 0.45,
                "operating_margin_ttm": 0.28,
                "roe": 0.25,
                "roic": 0.20,
                "free_cash_flow": 10_000_000_000,
                "debt_to_equity_raw": 280,
                "pe_ratio": 28,
                "ps_ratio": 7,
                "max_drawdown_1y": -0.15,
                "volatility_1y": 0.20,
                "price_vs_200dma": 0.05,
                "shares_growth_yoy": 0.01,
                "data_age_days": 2,
            }
        ]
        report = build_report(records, self.config, strategy_mode="stop_checking_price", min_score=0)
        self.assertEqual(len(report.hard_excluded), 0)
        candidate = report.candidates[0]
        self.assertEqual(candidate.ticker, "AAPL")
        self.assertAlmostEqual(candidate.company_snapshot["debt_to_equity_raw"], 280.0)
        self.assertAlmostEqual(candidate.company_snapshot["debt_to_equity_normalized"], 2.8)

    def test_stop_mode_sector_aware_financials_are_not_hard_excluded_for_debt(self) -> None:
        records = [
            {
                "ticker": "BANK",
                "company_name": "Bank Co",
                "sector": "Financials",
                "industry": "Banks",
                "price": 55,
                "market_cap": 30_000_000_000,
                "avg_dollar_volume_20d": 80_000_000,
                "revenue_growth_yoy": 0.08,
                "eps_growth_yoy": 0.09,
                "gross_margin_ttm": 0.42,
                "operating_margin_ttm": 0.20,
                "roe": 0.13,
                "roic": 0.09,
                "free_cash_flow": 2_000_000_000,
                "debt_to_equity_raw": 1200,
                "pe_ratio": 11,
                "ps_ratio": 2,
                "max_drawdown_1y": -0.12,
                "volatility_1y": 0.18,
                "price_vs_200dma": 0.02,
                "shares_growth_yoy": 0.01,
                "data_age_days": 2,
            }
        ]
        report = build_report(records, self.config, strategy_mode="stop_checking_price", min_score=0)
        self.assertEqual(len(report.hard_excluded), 0)
        self.assertEqual(report.candidates[0].ticker, "BANK")
        self.assertNotEqual(report.candidates[0].suggested_action, "EXCLUDE")

    def test_stop_mode_missing_critical_fields_caps_action_to_watchlist(self) -> None:
        records = [
            {
                "ticker": "MISS",
                "price": 120,
                "market_cap": 70_000_000_000,
                "avg_dollar_volume_20d": 150_000_000,
                "revenue_growth_yoy": 0.20,
                "eps_growth_yoy": 0.22,
                "gross_margin_ttm": 0.55,
                "operating_margin_ttm": 0.25,
                "roe": 0.18,
                "pe_ratio": 26,
                "ps_ratio": 6,
                "max_drawdown_1y": -0.16,
                "volatility_1y": 0.20,
                "price_vs_200dma": 0.03,
                "shares_growth_yoy": None,
                "roic": None,
                "free_cash_flow": None,
                "data_age_days": 2,
            }
        ]
        report = build_report(records, self.config, strategy_mode="stop_checking_price", min_score=0)
        candidate = report.candidates[0]
        self.assertLess(candidate.confidence_score or 0.0, 1.0)
        self.assertNotEqual(candidate.suggested_action, "WATCHLIST_HIGH_QUALITY")
        self.assertLessEqual(screener.STOP_ACTION_RANK[candidate.suggested_action], screener.STOP_ACTION_RANK["WATCHLIST"])

    def test_stop_mode_penalties_and_warnings(self) -> None:
        record = screener._canonicalize_record(
            {
                "ticker": "PEN",
                "price": 100,
                "market_cap": 50000000000,
                "avg_dollar_volume_20d": 140000000,
                "revenue_growth_yoy": -0.08,
                "eps_growth_yoy": -0.12,
                "gross_margin_ttm": 0.38,
                "operating_margin_ttm": -0.05,
                "net_margin_ttm": -0.02,
                "roe": 0.08,
                "roic": 0.05,
                "free_cash_flow": -100,
                "debt_to_equity": 3.0,
                "net_debt_to_ebitda": 5.0,
                "shares_growth_yoy": 0.08,
                "shares_growth_3y_cagr": 0.07,
                "pe_ratio": 18,
                "ps_ratio": 4,
                "max_drawdown_1y": -0.50,
                "volatility_1y": 0.50,
                "price_vs_200dma": -0.12,
                "data_age_days": 8,
            }
        )
        penalties, penalty_score = screener.calculate_stop_checking_price_penalties(record)
        self.assertGreater(penalty_score, 0)
        self.assertLessEqual(penalty_score, screener.MAX_STOP_CHECKING_PRICE_PENALTY)
        self.assertTrue(any(item["field"] == "free_cash_flow" for item in penalties))
        warnings = screener.generate_stop_checking_price_risk_warnings(record, 0.6)
        self.assertIn("自由現金流為負", "；".join(warnings))
        self.assertIn("資料不是最新", "；".join(warnings))

    def test_stop_mode_high_valuation_soft_penalties_are_not_zero(self) -> None:
        record = screener._canonicalize_record(
            {
                "ticker": "EXP",
                "price": 100,
                "market_cap": 50_000_000_000,
                "avg_dollar_volume_20d": 140_000_000,
                "revenue_growth_yoy": 0.20,
                "eps_growth_yoy": 0.20,
                "gross_margin_ttm": 0.55,
                "operating_margin_ttm": 0.22,
                "roe": 0.18,
                "roic": 0.13,
                "free_cash_flow": 2_000_000_000,
                "debt_to_equity": 2.2,
                "shares_growth_yoy": 0.06,
                "pe_ratio": 65,
                "forward_pe": 50,
                "ps_ratio": 24,
                "ev_to_ebitda": 38,
                "peg_ratio": 3.5,
                "max_drawdown_1y": -0.42,
                "volatility_1y": 0.85,
                "price_vs_200dma": 0.02,
                "data_age_days": 2,
            }
        )
        penalties, penalty_score = screener.calculate_stop_checking_price_penalties(record)
        fields = {item["field"] for item in penalties}
        self.assertGreater(penalty_score, 0)
        self.assertLessEqual(penalty_score, screener.MAX_STOP_CHECKING_PRICE_PENALTY)
        self.assertIn("pe_ratio", fields)
        self.assertIn("ps_ratio", fields)
        self.assertIn("shares_growth_yoy", fields)
        self.assertIn("volatility_1y", fields)

    def test_roic_missing_warning_does_not_claim_roic_is_good(self) -> None:
        records = [
            {
                "ticker": "ROEM",
                "price": 120,
                "market_cap": 70_000_000_000,
                "avg_dollar_volume_20d": 150_000_000,
                "revenue_growth_yoy": 0.20,
                "eps_growth_yoy": 0.22,
                "gross_margin_ttm": 0.55,
                "operating_margin_ttm": 0.25,
                "roe": 0.22,
                "roic": None,
                "free_cash_flow": 2_000_000_000,
                "shares_growth_yoy": 0.01,
                "pe_ratio": 26,
                "ps_ratio": 6,
                "max_drawdown_1y": -0.16,
                "volatility_1y": 0.20,
                "price_vs_200dma": 0.03,
                "data_age_days": 2,
            }
        ]
        report = build_report(records, self.config, strategy_mode="stop_checking_price", min_score=0)
        candidate = report.candidates[0]
        reason_text = "；".join(candidate.reasons)
        note_text = "；".join(candidate.confidence_notes)
        self.assertNotIn("ROE / ROIC", reason_text)
        self.assertNotIn("ROIC 表現良好", reason_text)
        self.assertIn("ROE 表現良好", reason_text)
        self.assertIn("ROIC 缺失，資本效率判斷主要依賴 ROE，需人工複查。", note_text)

    def test_missing_roic_with_roe_caps_at_watchlist_high_quality(self) -> None:
        record = {
            "ticker": "ROECAP",
            "price": 120,
            "market_cap": 70_000_000_000,
            "avg_dollar_volume_20d": 150_000_000,
            "revenue_growth_yoy": 0.20,
            "eps_growth_yoy": 0.22,
            "gross_margin_ttm": 0.55,
            "operating_margin_ttm": 0.25,
            "roe": 0.22,
            "roic": None,
            "free_cash_flow": 2_000_000_000,
            "shares_growth_yoy": 0.01,
            "pe_ratio": 26,
            "ps_ratio": 6,
            "max_drawdown_1y": -0.16,
            "volatility_1y": 0.20,
            "price_vs_200dma": 0.03,
            "data_age_days": 2,
        }
        report = build_report([record], self.config, strategy_mode="stop_checking_price", min_score=0, force_rebalance=True)
        candidate = report.candidates[0]
        self.assertLessEqual(screener.STOP_ACTION_RANK[candidate.suggested_action], screener.STOP_ACTION_RANK["WATCHLIST_HIGH_QUALITY"])
        self.assertIn("WATCHLIST_HIGH_QUALITY", candidate.action_cap_reason or "")

    def test_missing_roic_without_roe_caps_at_watchlist(self) -> None:
        record = {
            "ticker": "NOROE",
            "price": 120,
            "market_cap": 70_000_000_000,
            "avg_dollar_volume_20d": 150_000_000,
            "revenue_growth_yoy": 0.20,
            "eps_growth_yoy": 0.22,
            "gross_margin_ttm": 0.55,
            "operating_margin_ttm": 0.25,
            "roe": None,
            "roic": None,
            "free_cash_flow": 2_000_000_000,
            "shares_growth_yoy": 0.01,
            "pe_ratio": 26,
            "ps_ratio": 6,
            "max_drawdown_1y": -0.16,
            "volatility_1y": 0.20,
            "price_vs_200dma": 0.03,
            "data_age_days": 2,
        }
        report = build_report([record], self.config, strategy_mode="stop_checking_price", min_score=0, force_rebalance=True)
        candidate = report.candidates[0]
        self.assertLessEqual(screener.STOP_ACTION_RANK[candidate.suggested_action], screener.STOP_ACTION_RANK["WATCHLIST"])
        self.assertIn("roic", candidate.action_cap_reason or "")

    def test_missing_fcf_or_shares_growth_caps_at_watchlist(self) -> None:
        base = {
            "price": 120,
            "market_cap": 70_000_000_000,
            "avg_dollar_volume_20d": 150_000_000,
            "revenue_growth_yoy": 0.20,
            "eps_growth_yoy": 0.22,
            "gross_margin_ttm": 0.55,
            "operating_margin_ttm": 0.25,
            "roe": 0.22,
            "roic": 0.14,
            "pe_ratio": 26,
            "ps_ratio": 6,
            "max_drawdown_1y": -0.16,
            "volatility_1y": 0.20,
            "price_vs_200dma": 0.03,
            "data_age_days": 2,
        }
        records = [
            dict(base, ticker="NOFCF", free_cash_flow=None, shares_growth_yoy=0.01),
            dict(base, ticker="NOSHARES", free_cash_flow=2_000_000_000, shares_growth_yoy=None),
        ]
        report = build_report(records, self.config, strategy_mode="stop_checking_price", min_score=0, force_rebalance=True)
        self.assertTrue(
            all(screener.STOP_ACTION_RANK[item.suggested_action] <= screener.STOP_ACTION_RANK["WATCHLIST"] for item in report.candidates)
        )
        self.assertTrue(all(item.action_cap_reason for item in report.candidates))

    def test_price_history_251_low_severity_hidden_by_default(self) -> None:
        record = {
            "ticker": "HIST251",
            "price": 120,
            "market_cap": 70_000_000_000,
            "avg_dollar_volume_20d": 150_000_000,
            "revenue_growth_yoy": 0.20,
            "eps_growth_yoy": 0.22,
            "gross_margin_ttm": 0.55,
            "operating_margin_ttm": 0.25,
            "roe": 0.22,
            "roic": 0.14,
            "free_cash_flow": 2_000_000_000,
            "shares_growth_yoy": 0.01,
            "pe_ratio": 26,
            "ps_ratio": 6,
            "max_drawdown_1y": -0.16,
            "volatility_1y": 0.20,
            "price_vs_200dma": 0.03,
            "data_age_days": 2,
            "raw": {"history_points": 251},
        }
        report = build_report([record], self.config, strategy_mode="stop_checking_price", min_score=0)
        flags = "；".join(report.candidates[0].data_quality_flags)
        self.assertNotIn("價格歷史長度不足 252", flags)

    def test_debt_to_equity_normalization_goes_to_notes(self) -> None:
        record = {
            "ticker": "DENORM",
            "price": 120,
            "market_cap": 70_000_000_000,
            "avg_dollar_volume_20d": 150_000_000,
            "revenue_growth_yoy": 0.20,
            "eps_growth_yoy": 0.22,
            "gross_margin_ttm": 0.55,
            "operating_margin_ttm": 0.25,
            "roe": 0.22,
            "roic": 0.14,
            "free_cash_flow": 2_000_000_000,
            "shares_growth_yoy": 0.01,
            "debt_to_equity_raw": 66.509,
            "pe_ratio": 26,
            "ps_ratio": 6,
            "max_drawdown_1y": -0.16,
            "volatility_1y": 0.20,
            "price_vs_200dma": 0.03,
            "data_age_days": 2,
        }
        report = build_report([record], self.config, strategy_mode="stop_checking_price", min_score=0)
        candidate = report.candidates[0]
        self.assertFalse(any("debt_to_equity 已由 raw" in flag for flag in candidate.data_quality_flags))
        self.assertTrue(any("debt_to_equity 已由 raw" in note for note in candidate.normalization_notes))

    def test_borderline_dilution_exclusion_is_marked(self) -> None:
        record = {
            "ticker": "DILUTE",
            "price": 120,
            "market_cap": 70_000_000_000,
            "avg_dollar_volume_20d": 150_000_000,
            "revenue_growth_yoy": 0.20,
            "eps_growth_yoy": 0.22,
            "gross_margin_ttm": 0.55,
            "operating_margin_ttm": 0.25,
            "roe": 0.22,
            "roic": 0.14,
            "free_cash_flow": 2_000_000_000,
            "shares_growth_yoy": 0.2009,
            "pe_ratio": 26,
            "ps_ratio": 6,
            "max_drawdown_1y": -0.16,
            "volatility_1y": 0.20,
            "price_vs_200dma": 0.03,
            "data_age_days": 2,
        }
        report = build_report([record], self.config, strategy_mode="stop_checking_price", min_score=0)
        detail = report.hard_excluded[0].exclusion_details[0]
        self.assertEqual(detail.get("severity"), "borderline")
        self.assertIn("邊界剔除", detail.get("reason", ""))

    def test_stop_mode_extra_filter_and_quarterly_action(self) -> None:
        good_record = {
            "ticker": "LONG",
            "price": 120,
            "market_cap": 60000000000,
            "avg_dollar_volume_20d": 150000000,
            "revenue_growth_yoy": 0.20,
            "eps_growth_yoy": 0.22,
            "gross_margin_ttm": 0.55,
            "operating_margin_ttm": 0.25,
            "roe": 0.18,
            "roic": 0.14,
            "free_cash_flow": 2500000000,
            "debt_to_equity": 0.8,
            "shares_growth_yoy": 0.01,
            "pe_ratio": 24,
            "ps_ratio": 6,
            "max_drawdown_1y": -0.16,
            "volatility_1y": 0.20,
            "price_vs_200dma": 0.03,
            "price_vs_sma50_pct": 2,
            "price_vs_sma200_pct": 4,
            "data_age_days": 31,
        }
        hybrid_report = build_report([good_record], ScreenConfig(max_data_age_days=365), strategy_mode="hybrid")
        self.assertEqual(len(hybrid_report.candidates), 1)
        stop_report = build_report([good_record], ScreenConfig(max_data_age_days=365), strategy_mode="stop_checking_price")
        self.assertEqual(len(stop_report.candidates), 0)
        self.assertEqual(len(stop_report.excluded), 1)
        self.assertIn("價格資料超過 30 天，過舊", stop_report.excluded[0].excluded_reason or "")
        self.assertEqual(
            screener.assign_stop_checking_price_action(90, 0.95, False, False),
            "WATCHLIST_HIGH_QUALITY",
        )
        self.assertEqual(
            screener.assign_stop_checking_price_action(90, 0.95, True, False),
            "BUY_CANDIDATE",
        )
        self.assertEqual(
            screener.assign_stop_checking_price_action(90, 0.95, False, True),
            "EXCLUDE",
        )

    def test_split_data_age_uses_price_age_for_hard_exclusion(self) -> None:
        record = {
            "ticker": "FRESH",
            "price": 120,
            "market_cap": 60000000000,
            "avg_dollar_volume_20d": 150000000,
            "revenue_growth_yoy": 0.20,
            "eps_growth_yoy": 0.22,
            "gross_margin_ttm": 0.55,
            "operating_margin_ttm": 0.25,
            "roe": 0.18,
            "roic": 0.14,
            "free_cash_flow": 2500000000,
            "debt_to_equity": 0.8,
            "shares_growth_yoy": 0.01,
            "pe_ratio": 24,
            "ps_ratio": 6,
            "max_drawdown_1y": -0.16,
            "volatility_1y": 0.20,
            "price_vs_200dma": 0.03,
            "price_data_age_days": 1,
            "fundamental_data_age_days": 90,
            "shares_data_age_days": 90,
        }
        report = build_report([record], self.config, strategy_mode="stop_checking_price", min_score=0)
        self.assertEqual(len(report.candidates), 1)
        note_text = "；".join(report.candidates[0].confidence_notes)
        self.assertIn("財報資料已 90 天未更新", note_text)
        self.assertIn("股本資料已 90 天未更新", note_text)

    def test_stop_mode_ranking_prefers_quality_over_momentum(self) -> None:
        records = [
            {
                "ticker": "QUALA",
                "price": 100,
                "market_cap": 50000000000,
                "avg_dollar_volume_20d": 140000000,
                "revenue_growth_yoy": 0.18,
                "eps_growth_yoy": 0.20,
                "gross_margin_ttm": 0.57,
                "operating_margin_ttm": 0.24,
                "roe": 0.20,
                "roic": 0.15,
                "free_cash_flow": 2600000000,
                "debt_to_equity": 0.7,
                "shares_growth_yoy": 0.01,
                "pe_ratio": 26,
                "ps_ratio": 6,
                "ev_to_ebitda": 15,
                "relative_strength_252d": 58,
                "price_vs_200dma": 0.02,
                "price_vs_sma50_pct": 1,
                "price_vs_sma200_pct": 3,
                "max_drawdown_1y": -0.14,
                "volatility_1y": 0.18,
                "data_age_days": 2,
            },
            {
                "ticker": "MOMO",
                "price": 100,
                "market_cap": 50000000000,
                "avg_dollar_volume_20d": 140000000,
                "revenue_growth_yoy": 0.02,
                "eps_growth_yoy": 0.01,
                "gross_margin_ttm": 0.20,
                "operating_margin_ttm": 0.05,
                "roe": 0.06,
                "roic": 0.04,
                "free_cash_flow": -50000000,
                "debt_to_equity": 3.0,
                "shares_growth_yoy": 0.09,
                "pe_ratio": 18,
                "ps_ratio": 3,
                "ev_to_ebitda": 10,
                "relative_strength_252d": 96,
                "price_vs_200dma": 0.18,
                "price_vs_sma50_pct": 14,
                "price_vs_sma200_pct": 20,
                "max_drawdown_1y": -0.45,
                "volatility_1y": 0.46,
                "data_age_days": 2,
            },
        ]
        report = build_report(records, ScreenConfig(max_data_age_days=365), strategy_mode="stop_checking_price", min_score=0)
        self.assertEqual([item.ticker for item in report.candidates], ["QUALA", "MOMO"])
        self.assertGreater(report.candidates[0].total_score or 0, report.candidates[1].total_score or 0)
        self.assertIn(report.candidates[0].suggested_action, {"WATCHLIST", "WATCHLIST_HIGH_QUALITY"})

    def test_yfinance_snapshot_builder_normalizes_payload(self) -> None:
        start = date(2025, 1, 1)
        history = [
            {
                "Date": (start + timedelta(days=day - 1)).isoformat(),
                "Close": 99 + day,
                "Volume": 1_000_000 + day * 10_000,
            }
            for day in range(1, 61)
        ]
        provider = FakeProvider(
            {
                "AAA": {
                    "info": {
                        "regularMarketPrice": 159,
                        "marketCap": 10000000000,
                        "revenueGrowth": 0.15,
                        "earningsGrowth": 0.20,
                        "grossMargins": 0.40,
                        "operatingMargins": 0.25,
                        "returnOnEquity": 0.18,
                        "trailingPE": 24,
                        "priceToSalesTrailing12Months": 5,
                        "beta": 1.1,
                        "debtToEquity": 0.8,
                        "quoteType": "EQUITY",
                        "fullExchangeName": "NASDAQ",
                    },
                    "history": history,
                }
            }
        )
        bundle = fetch_snapshot(["AAA"], provider=provider, as_of=date(2025, 3, 1))
        record = bundle.records[0]
        self.assertEqual(record["ticker"], "AAA")
        self.assertEqual(record["price"], 159.0)
        self.assertEqual(record["market_cap"], 10000000000)
        self.assertEqual(record["avg_volume_20d"], 1_505_000)
        self.assertEqual(record["revenue_growth_yoy"], 15.0)
        self.assertEqual(record["eps_growth_yoy"], 20.0)
        self.assertAlmostEqual(record["price_vs_sma50_pct"], 18.216, places=2)
        self.assertIn("common_stock", record["security_type"])

    def test_yfinance_retry_success_and_failed_counts(self) -> None:
        history = [
            {"Date": "2025-01-01", "Close": 100, "Volume": 1_000_000},
            {"Date": "2025-01-02", "Close": 101, "Volume": 1_100_000},
        ]
        success_payload = {
            "info": {
                "regularMarketPrice": 101,
                "marketCap": 10_000_000_000,
                "quoteType": "EQUITY",
            },
            "history": history,
        }
        retry_provider = RetryProvider(2, success_payload)
        retry_bundle = fetch_snapshot(["AAA"], provider=retry_provider, as_of=date(2025, 3, 1), retry_attempts=2)
        self.assertEqual(retry_provider.calls, 3)
        self.assertEqual(retry_bundle.retry_failed_count, 0)
        self.assertEqual(retry_bundle.fetch_failed_count, 0)
        self.assertEqual(retry_bundle.records[0]["raw"]["retry_count"], 2)

        failed_provider = CountingPayloadProvider({"info": {}, "history": [], "errors": ["curl timeout"]})
        failed_bundle = fetch_snapshot(["BBB"], provider=failed_provider, as_of=date(2025, 3, 1), retry_attempts=2)
        self.assertEqual(failed_provider.calls, 3)
        self.assertEqual(failed_bundle.retry_failed_count, 1)
        self.assertEqual(failed_bundle.fetch_failed_count, 1)
        self.assertTrue(failed_bundle.records[0]["raw"]["fetch_failed"])

    def test_yfinance_snapshot_roundtrip(self) -> None:
        bundle = SnapshotBundle(
            source_name="yfinance",
            as_of="2025-01-06",
            fetched_at="2025-01-06T08:00:00",
            records=[{"ticker": "AAPL", "price": 200, "market_cap": 3000000000000, "avg_dollar_volume_20d": 8000000000}],
            warnings=["AAPL: test warning"],
            errors=[],
            universe=["AAPL"],
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "snapshot.json"
            saved = save_snapshot(bundle, path)
            loaded = load_snapshot(saved)
        self.assertEqual(loaded.source_name, "yfinance")
        self.assertEqual(loaded.records[0]["ticker"], "AAPL")
        self.assertEqual(loaded.warnings, ["AAPL: test warning"])

    def test_market_context_sidecar_roundtrip(self) -> None:
        provider = FakeProvider(
            {
                "SPY": {
                    "info": {"regularMarketPrice": 610, "marketCap": 1, "quoteType": "ETF"},
                    "history": [
                        {"Date": "2026-06-25", "Close": 600, "Volume": 1},
                        {"Date": "2026-06-26", "Close": 605, "Volume": 1},
                        {"Date": "2026-06-27", "Close": 610, "Volume": 1},
                    ]
                    + [{"Date": f"2025-12-{day:02d}", "Close": 500 + day, "Volume": 1} for day in range(1, 29)],
                    "errors": [],
                },
                "QQQ": {
                    "info": {"regularMarketPrice": 530, "marketCap": 1, "quoteType": "ETF"},
                    "history": [
                        {"Date": "2026-06-25", "Close": 520, "Volume": 1},
                        {"Date": "2026-06-26", "Close": 525, "Volume": 1},
                        {"Date": "2026-06-27", "Close": 530, "Volume": 1},
                    ]
                    + [{"Date": f"2025-11-{day:02d}", "Close": 430 + day, "Volume": 1} for day in range(1, 29)],
                    "errors": [],
                },
                "^VIX": {
                    "info": {"regularMarketPrice": 18, "marketCap": 1, "quoteType": "INDEX"},
                    "history": [
                        {"Date": "2026-06-25", "Close": 19, "Volume": 1},
                        {"Date": "2026-06-26", "Close": 18.5, "Volume": 1},
                        {"Date": "2026-06-27", "Close": 18, "Volume": 1},
                    ]
                    + [{"Date": f"2025-10-{day:02d}", "Close": 21 + day * 0.1, "Volume": 1} for day in range(1, 29)],
                    "errors": [],
                },
            }
        )
        records = [
            {"ticker": "AAA", "price_vs_sma200_pct": 5},
            {"ticker": "BBB", "price_vs_sma200_pct": -2},
            {"ticker": "CCC", "price_vs_sma200_pct": 1},
        ]
        context = build_market_context(records, provider=provider)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "context.json"
            saved = save_market_context(context, path)
            loaded = load_market_context(saved)
        self.assertEqual(loaded["as_of_date"], "2026-06-27")
        self.assertEqual(loaded["breadth_eligible_count"], 3)
        self.assertAlmostEqual(loaded["breadth_above_200dma"], 0.667, places=3)

    def test_market_regime_risk_on_hybrid_effective_weights(self) -> None:
        records = [
            self._sector_preview_record("AAA", revenue_growth_yoy=5, eps_growth_yoy=5, relative_strength_252d=80),
            self._sector_preview_record("BBB", revenue_growth_yoy=15, eps_growth_yoy=15, relative_strength_252d=40),
        ]
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=2, market_context=self._market_context("risk_on"))
        self.assertEqual(report.market_regime, "risk_on")
        self.assertEqual(report.market_regime_status, "enabled")
        self.assertEqual(report.effective_composite_weights, {"fundamental": 0.4, "momentum": 0.4, "risk_safety": 0.2})

    def test_market_regime_risk_off_hybrid_effective_weights(self) -> None:
        records = [
            self._sector_preview_record("AAA"),
            self._sector_preview_record("BBB", revenue_growth_yoy=20, eps_growth_yoy=20),
        ]
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=2, market_context=self._market_context("risk_off"))
        self.assertEqual(report.market_regime, "risk_off")
        self.assertEqual(report.effective_composite_weights, {"fundamental": 0.35, "momentum": 0.3, "risk_safety": 0.35})

    def test_market_regime_neutral_keeps_scores_unchanged(self) -> None:
        records = [self._sector_preview_record("AAA"), self._sector_preview_record("BBB", revenue_growth_yoy=20, eps_growth_yoy=20)]
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=2, market_context=self._market_context("neutral"))
        self.assertEqual(report.market_regime, "neutral")
        self.assertEqual(report.market_regime_status, "enabled")
        self.assertTrue(all(item.total_score == item.base_total_score for item in report.candidates))
        self.assertTrue(all(item.market_regime_score_delta == 0 for item in report.candidates))

    def test_market_regime_insufficient_keeps_scores_unchanged(self) -> None:
        records = [self._sector_preview_record("AAA"), self._sector_preview_record("BBB", revenue_growth_yoy=20, eps_growth_yoy=20)]
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=2, market_context=self._market_context("insufficient"))
        self.assertEqual(report.market_regime, "neutral")
        self.assertEqual(report.market_regime_status, "insufficient_market_data")
        self.assertTrue(all(item.total_score == item.base_total_score for item in report.candidates))
        self.assertTrue(all(item.market_regime_score_delta == 0 for item in report.candidates))

    def test_market_regime_stop_risk_on_keeps_weights(self) -> None:
        records = [
            self._sector_preview_record("STOPA", free_cash_flow=2_000_000_000, shares_growth_yoy=0.01, roe=0.20, roic=0.14),
            self._sector_preview_record("STOPB", free_cash_flow=1_000_000_000, shares_growth_yoy=0.02, roe=0.12, roic=0.08),
        ]
        report = build_report(records, self.config, strategy_mode="stop_checking_price", min_score=0, top_n=2, market_context=self._market_context("risk_on"))
        self.assertEqual(report.effective_composite_weights, {"fundamental": 0.55, "risk_safety": 0.3, "momentum": 0.15})

    def test_market_regime_stop_risk_off_uses_penalty_confidence_pipeline(self) -> None:
        records = [
            self._sector_preview_record(
                "STOPA",
                free_cash_flow=-100,
                shares_growth_yoy=0.08,
                roe=0.20,
                roic=0.14,
                debt_to_equity=3.0,
                volatility_63d=55,
                max_drawdown_252d=45,
            ),
            self._sector_preview_record("STOPB", free_cash_flow=1_000_000_000, shares_growth_yoy=0.02, roe=0.12, roic=0.08),
        ]
        report = build_report(records, self.config, strategy_mode="stop_checking_price", min_score=0, top_n=2, market_context=self._market_context("risk_off"))
        candidate = next(item for item in report.candidates if item.ticker == "STOPA")
        expected_raw = screener.weighted_average_available(
            {
                "fundamental": candidate.fundamental_score,
                "momentum": candidate.momentum_score,
                "risk_safety": candidate.risk_safety_score,
            },
            report.effective_composite_weights,
        )
        expected_adjusted = max(0.0, min(100.0, max(0.0, (expected_raw or 0.0) - (candidate.penalty_score or 0.0)) * (candidate.confidence_multiplier or 1.0)))
        self.assertEqual(candidate.raw_score, round(expected_raw or 0.0, 1))
        self.assertEqual(candidate.total_score, round(expected_adjusted, 1))
        self.assertEqual(candidate.final_score, round(expected_adjusted, 1))

    def test_market_regime_effective_weights_sum_to_one(self) -> None:
        for strategy_mode in ("hybrid", "stop_checking_price"):
            for regime in ("risk_on", "neutral", "risk_off"):
                report = build_report(
                    [self._sector_preview_record("AAA"), self._sector_preview_record("BBB", revenue_growth_yoy=20, eps_growth_yoy=20)],
                    self.config,
                    strategy_mode=strategy_mode,
                    top_n=2,
                    min_score=0 if strategy_mode == "stop_checking_price" else None,
                    market_context=self._market_context(regime),
                )
                self.assertAlmostEqual(sum(report.effective_composite_weights.values()), 1.0, places=9)

    def test_market_regime_coverage_gate_legacy_path_can_still_change_final_score(self) -> None:
        records = [
            self._sector_preview_record("HASMETA", sector="Technology", industry="Software"),
            self._sector_preview_record("NOMETA1", sector=None, industry=None, revenue_growth_yoy=30),
            self._sector_preview_record("NOMETA2", sector=None, industry=None, revenue_growth_yoy=1),
        ]
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=3, market_context=self._market_context("risk_off"))
        self.assertEqual(report.sector_aware_status, "disabled_insufficient_sector_metadata")
        self.assertTrue(all(item.official_score_source == "legacy_metadata_gate" for item in report.candidates))
        self.assertTrue(any(item.total_score != item.base_total_score for item in report.candidates))

    def test_market_regime_missing_metadata_keeps_preview_out_of_final_score(self) -> None:
        records = [
            self._sector_preview_record("FULLHIGH", revenue_growth_yoy=28, eps_growth_yoy=28, relative_strength_252d=82),
            self._sector_preview_record("FULLMID", revenue_growth_yoy=9, eps_growth_yoy=9, relative_strength_252d=56),
            self._sector_preview_record("NOMETA", sector=None, industry=None, revenue_growth_yoy=12, eps_growth_yoy=12, relative_strength_252d=72),
        ]
        records.extend(self._sector_preview_record(f"WEAK{i:02d}", sector="Technology", industry="Hardware", revenue_growth_yoy=-5 + i, eps_growth_yoy=-4 + i, relative_strength_252d=20 + i) for i in range(9))
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=12, market_context=self._market_context("risk_on"))
        nometa = next(item for item in report.candidates if item.ticker == "NOMETA")
        fullmid = next(item for item in report.candidates if item.ticker == "FULLMID")
        self.assertEqual(nometa.official_score_source, "legacy_missing_metadata")
        expected_total = screener.weighted_average_available(
            {
                "fundamental": nometa.fundamental_score,
                "momentum": nometa.momentum_score,
                "risk_safety": nometa.risk_safety_score,
            },
            report.effective_composite_weights,
        )
        self.assertEqual(nometa.total_score, round(expected_total or 0.0, 1))
        self.assertEqual(nometa.suggested_action, screener.assign_hybrid_action(nometa.record, nometa.total_score, nometa.risk_safety_score)[0])
        self.assertEqual(fullmid.official_score_source, "sector_aware")

    def test_market_regime_without_sidecar_is_neutral_and_no_fetch(self) -> None:
        records = [self._sector_preview_record("AAA"), self._sector_preview_record("BBB", revenue_growth_yoy=20, eps_growth_yoy=20)]
        report = build_report(records, self.config, strategy_mode="hybrid", top_n=2)
        self.assertEqual(report.market_regime, "neutral")
        self.assertEqual(report.market_regime_status, "insufficient_market_data")
        self.assertTrue(all(item.total_score == item.base_total_score for item in report.candidates))

    def test_backtest_fails_when_filename_date_mismatches_payload_as_of(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            payload = {
                "metadata": {"as_of": "2026-01-30"},
                "records": [self._backtest_record("AAA", 100)],
            }
            (root / "2026-01-31.json").write_text(json.dumps(payload))
            with self.assertRaises(ValueError):
                backtest.load_historical_snapshots(root)

    def test_backtest_monthly_uses_last_available_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_backtest_snapshot(root, "2026-01-10", [self._backtest_record("AAA", 100)])
            self._write_backtest_snapshot(root, "2026-01-31", [self._backtest_record("AAA", 101)])
            self._write_backtest_snapshot(root, "2026-02-15", [self._backtest_record("AAA", 102)])
            self._write_backtest_snapshot(root, "2026-02-28", [self._backtest_record("AAA", 103)])
            snapshots = backtest.load_historical_snapshots(root)
            selected = backtest.select_rebalance_snapshots(snapshots, "hybrid")
            self.assertEqual([item.as_of.isoformat() for item in selected], ["2026-01-31", "2026-02-28"])

    def test_backtest_quarterly_uses_last_available_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_backtest_snapshot(root, "2026-01-31", [self._backtest_record("AAA", 100)])
            self._write_backtest_snapshot(root, "2026-03-31", [self._backtest_record("AAA", 101)])
            self._write_backtest_snapshot(root, "2026-04-30", [self._backtest_record("AAA", 102)])
            self._write_backtest_snapshot(root, "2026-06-30", [self._backtest_record("AAA", 103)])
            snapshots = backtest.load_historical_snapshots(root)
            selected = backtest.select_rebalance_snapshots(snapshots, "stop_checking_price")
            self.assertEqual([item.as_of.isoformat() for item in selected], ["2026-03-31", "2026-06-30"])

    def test_backtest_top_decile_uses_floor_and_minimum_one(self) -> None:
        portfolios = backtest._configured_portfolios("hybrid", 9)
        self.assertIn((backtest.TOP_DECILE, 1), portfolios)
        portfolios = backtest._configured_portfolios("hybrid", 23)
        self.assertIn((backtest.TOP_DECILE, 2), portfolios)

    def test_backtest_missing_next_price_invalidates_period(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            formation = [
                self._backtest_record("AAA", 100, relative_strength_252d=90),
                self._backtest_record("BBB", 100, relative_strength_252d=80),
            ]
            next_records = [self._backtest_record("AAA", 110)]
            self._write_backtest_snapshot(root, "2026-01-31", formation, benchmarks={"SPY": 500})
            self._write_backtest_snapshot(root, "2026-02-28", next_records, benchmarks={"SPY": 510})
            snapshots = backtest.load_historical_snapshots(root)
            summaries = backtest.run_backtest(
                snapshots,
                "hybrid",
                config=self.config,
                spy_prices={"2026-01-31": 500, "2026-02-28": 510},
            )
            top20 = next(item for item in summaries if item.portfolio_name == backtest.TOP_20)
            self.assertEqual(top20.missing_return_period_count, 1)
            self.assertEqual(top20.missing_return_ticker_count, 1)
            self.assertEqual(top20.missing_return_tickers, ["BBB"])
            self.assertIsNone(top20.periods[0].return_value)
            self.assertIsNone(top20.cagr)

    def test_backtest_turnover_uses_half_absolute_weight_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            jan = [
                self._backtest_record("AAA", 100, relative_strength_252d=90),
                self._backtest_record("BBB", 100, relative_strength_252d=80),
                self._backtest_record("CCC", 100, relative_strength_252d=70),
            ]
            feb = [
                self._backtest_record("CCC", 100, relative_strength_252d=95),
                self._backtest_record("BBB", 100, relative_strength_252d=85),
                self._backtest_record("AAA", 100, relative_strength_252d=65),
            ]
            mar = [
                self._backtest_record("CCC", 110, relative_strength_252d=95),
                self._backtest_record("DDD", 100, relative_strength_252d=90),
                self._backtest_record("BBB", 110, relative_strength_252d=80),
            ]
            self._write_backtest_snapshot(root, "2026-01-31", jan, benchmarks={"SPY": 500})
            self._write_backtest_snapshot(root, "2026-02-28", feb, benchmarks={"SPY": 510})
            self._write_backtest_snapshot(root, "2026-03-31", mar, benchmarks={"SPY": 520})
            snapshots = backtest.load_historical_snapshots(root)
            summaries = backtest.run_backtest(
                snapshots,
                "hybrid",
                config=self.config,
                spy_prices={"2026-01-31": 500, "2026-02-28": 510, "2026-03-31": 520},
            )
            top_decile = next(item for item in summaries if item.portfolio_name == backtest.TOP_DECILE)
            self.assertIsNone(top_decile.periods[0].turnover)
            self.assertEqual(top_decile.periods[1].turnover, 1.0)

    def test_backtest_spy_benchmark_missing_price_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_backtest_snapshot(root, "2026-01-31", [self._backtest_record("AAA", 100)])
            self._write_backtest_snapshot(root, "2026-02-28", [self._backtest_record("AAA", 110)])
            snapshots = backtest.load_historical_snapshots(root)
            summaries = backtest.run_backtest(
                snapshots,
                "hybrid",
                config=self.config,
                spy_prices={"2026-01-31": 500},
            )
            top20 = next(item for item in summaries if item.portfolio_name == backtest.TOP_20)
            self.assertIsNone(top20.periods[0].benchmark_spy_return)
            self.assertEqual(top20.periods[0].benchmark_missing_reason, "missing_spy_price")

    def test_backtest_equal_weight_universe_uses_only_stocks_with_next_price(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            jan = [
                self._backtest_record("AAA", 100),
                self._backtest_record("BBB", 100),
                self._backtest_record("CCC", 100),
            ]
            feb = [
                self._backtest_record("AAA", 110),
                self._backtest_record("BBB", 90),
            ]
            self._write_backtest_snapshot(root, "2026-01-31", jan, benchmarks={"SPY": 500})
            self._write_backtest_snapshot(root, "2026-02-28", feb, benchmarks={"SPY": 510})
            snapshots = backtest.load_historical_snapshots(root)
            summaries = backtest.run_backtest(
                snapshots,
                "hybrid",
                config=self.config,
                spy_prices={"2026-01-31": 500, "2026-02-28": 510},
            )
            top20 = next(item for item in summaries if item.portfolio_name == backtest.TOP_20)
            self.assertEqual(top20.periods[0].universe_benchmark_eligible_count, 2)
            self.assertAlmostEqual(top20.periods[0].benchmark_universe_return, 0.0)

    def test_backtest_outputs_include_research_flags_and_do_not_use_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            out = root / "outputs" / "hybrid-backtest"
            self._write_backtest_snapshot(root, "2026-01-31", [self._backtest_record("AAA", 100)], benchmarks={"SPY": 500})
            self._write_backtest_snapshot(root, "2026-02-28", [self._backtest_record("AAA", 110)], benchmarks={"SPY": 510})
            with mock.patch("socket.socket", side_effect=AssertionError("network should not be used")):
                exit_code = backtest.main(
                    [
                        "--snapshots-dir",
                        str(root),
                        "--strategy-mode",
                        "hybrid",
                        "--output-prefix",
                        str(out),
                    ]
                )
            self.assertEqual(exit_code, 0)
            summary_csv = out.with_suffix(".summary.csv").read_text()
            markdown = out.with_suffix(".md").read_text()
            self.assertIn("research_only", summary_csv)
            self.assertIn("not_point_in_time_accurate", summary_csv)
            self.assertIn("survivorship_bias_possible", summary_csv)
            self.assertIn("not_for_automated_trading", summary_csv)
            self.assertIn("missing_return_policy", summary_csv)
            self.assertIn("research_only: `true`", markdown)
            self.assertIn("missing_return_policy: `invalidate_portfolio_period`", markdown)

    def test_web_parse_uploaded_ticker_csv(self) -> None:
        kind, rows = parse_uploaded_content("watchlist.csv", "ticker\nAAPL\nMSFT\n")
        self.assertEqual(kind, "tickers")
        self.assertEqual(rows, ["AAPL", "MSFT"])

    def test_web_parse_uploaded_record_csv(self) -> None:
        content = "ticker,price,market_cap,avg_dollar_volume_20d\nAAPL,180,3000000000000,1000000000\n"
        kind, rows = parse_uploaded_content("snapshot.csv", content)
        self.assertEqual(kind, "records")
        self.assertEqual(rows[0]["ticker"], "AAPL")
        self.assertEqual(rows[0]["price"], "180")

    def test_web_sample_universe_request_is_offline(self) -> None:
        payload = {
            "source_mode": "sample_universe",
            "strategy_mode": "hybrid",
            "force_rebalance": False,
            "auto_fetch": False,
        }
        report = run_screen_request(payload)
        self.assertEqual(report["source_name"], "離線示範資料")
        self.assertEqual(report["strategy_mode"], "hybrid")
        self.assertGreaterEqual(report["candidate_count"], 1)

    def test_web_uploaded_records_can_screen_without_fetch(self) -> None:
        content = "ticker,price,market_cap,avg_dollar_volume_20d\nAAPL,180,3000000000000,1000000000\n"
        payload = {
            "source_mode": "uploaded",
            "filename": "snapshot.csv",
            "content": content,
            "strategy_mode": "hybrid",
            "auto_fetch": False,
        }
        report = run_screen_request(payload)
        self.assertEqual(report["source_name"], "snapshot.csv")
        self.assertEqual(report["universe_size"], 1)


if __name__ == "__main__":
    unittest.main()
