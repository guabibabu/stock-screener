#!/usr/bin/env python3
"""Deterministic tests for the US stock screener."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
import sys


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import us_stock_screener as screener
from us_stock_screener import ScreenConfig, build_report, load_records, screen_records
from fetch_yfinance_snapshot import (
    SnapshotBundle,
    fetch_snapshot,
    load_snapshot,
    load_watchlist,
    save_snapshot,
)
from us_stock_screener_web import parse_uploaded_content, run_screen_request


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

    def test_sample_csv_loads(self) -> None:
        sample = SCRIPT_DIR.parent / "references" / "sample-universe.csv"
        records = load_records(sample)
        self.assertEqual(len(records), 4)
        self.assertEqual(records[0].ticker, "AAPL")

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
        self.assertEqual(candidate.suggested_action, "WATCHLIST_HIGH_QUALITY")
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
        self.assertIn(candidate.suggested_action, {"WATCHLIST", "WATCHLIST_DATA_INSUFFICIENT"})

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
        self.assertEqual(report.candidates[0].suggested_action, "WATCHLIST_HIGH_QUALITY")

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
