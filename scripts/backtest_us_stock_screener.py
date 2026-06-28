#!/usr/bin/env python3
"""Research-only backtest runner for the US stock screener."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from statistics import mean, stdev
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from us_stock_screener import ScreenConfig, build_report, load_market_context, load_records, score_record


SNAPSHOT_FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")
RESEARCH_FLAGS = {
    "research_only": True,
    "not_point_in_time_accurate": True,
    "survivorship_bias_possible": True,
    "not_for_automated_trading": True,
    "missing_return_policy": "invalidate_portfolio_period",
    "allow_unverified_historical": False,
}
TOP_20 = "top_20"
TOP_50 = "top_50"
TOP_DECILE = "top_decile"
SPY_BENCHMARK = "SPY"
UNIVERSE_BENCHMARK = "equal_weight_universe"


@dataclass
class HistoricalSnapshot:
    as_of: date
    path: Path
    payload: Dict[str, Any]
    records: List[Any]
    market_context: Optional[Dict[str, Any]]
    point_in_time_verified: bool


@dataclass
class PortfolioPeriod:
    formation_date: date
    next_date: date
    portfolio_name: str
    holdings: List[str]
    holding_count: int
    return_value: Optional[float]
    benchmark_spy_return: Optional[float]
    benchmark_universe_return: Optional[float]
    turnover: Optional[float]
    top_10_weight: Optional[float]
    sector_exposure: Dict[str, float]
    missing_return_tickers: List[str] = field(default_factory=list)
    period_status: str = "valid"
    benchmark_missing_reason: Optional[str] = None
    universe_benchmark_eligible_count: int = 0
    benchmark_spy_status: str = "valid"
    benchmark_universe_status: str = "valid"
    holding_returns: Dict[str, float] = field(default_factory=dict)


@dataclass
class BacktestSummary:
    strategy_mode: str
    portfolio_name: str
    rebalance_frequency: str
    periods: List[PortfolioPeriod]
    annualization_factor: int
    missing_return_period_count: int
    missing_return_ticker_count: int
    missing_return_tickers: List[str]
    total_formations: int
    expected_period_count: int
    valid_period_count: int
    invalid_period_count: int
    no_candidate_period_count: int
    coverage_ratio: float
    metric_status: str
    cagr: Optional[float]
    annualized_volatility: Optional[float]
    sharpe_ratio: Optional[float]
    sortino_ratio: Optional[float]
    sortino_reason: str
    max_drawdown: Optional[float]
    average_turnover: Optional[float]
    hit_rate: Optional[float]
    average_holding_period: Optional[float]
    average_sector_exposure: Dict[str, float]
    average_top_holdings_concentration: Optional[float]
    benchmark_spy_expected_period_count: int
    benchmark_spy_valid_period_count: int
    benchmark_spy_coverage_ratio: float
    benchmark_spy_metric_status: str
    benchmark_spy_cagr: Optional[float]
    benchmark_spy_annualized_volatility: Optional[float]
    benchmark_spy_sharpe_ratio: Optional[float]
    benchmark_spy_sortino_ratio: Optional[float]
    benchmark_spy_sortino_reason: str
    benchmark_spy_max_drawdown: Optional[float]
    benchmark_universe_expected_period_count: int
    benchmark_universe_valid_period_count: int
    benchmark_universe_coverage_ratio: float
    benchmark_universe_metric_status: str
    benchmark_universe_cagr: Optional[float]
    benchmark_universe_annualized_volatility: Optional[float]
    benchmark_universe_sharpe_ratio: Optional[float]
    benchmark_universe_sortino_ratio: Optional[float]
    benchmark_universe_sortino_reason: str
    benchmark_universe_max_drawdown: Optional[float]


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _safe_round(value: Optional[float], digits: int = 6) -> Optional[float]:
    if value is None:
        return None
    return round(value, digits)


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc.msg}") from exc


def _extract_snapshot_payload(path: Path) -> Dict[str, Any]:
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"Snapshot {path.name} must be a JSON object")
    return payload


def _validate_snapshot_date(path: Path, payload: Dict[str, Any]) -> date:
    filename_date = date.fromisoformat(path.stem)
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        if metadata.get("as_of"):
            payload_date = date.fromisoformat(str(metadata["as_of"]))
            if payload_date != filename_date:
                raise ValueError(
                    f"Snapshot {path.name} has metadata.as_of={payload_date.isoformat()} "
                    f"which does not match filename date {filename_date.isoformat()}"
                )
        if metadata.get("requested_as_of"):
            requested_date = date.fromisoformat(str(metadata["requested_as_of"]))
            if requested_date != filename_date:
                raise ValueError(
                    f"Snapshot {path.name} has metadata.requested_as_of={requested_date.isoformat()} "
                    f"which does not match filename date {filename_date.isoformat()}"
                )
    return filename_date


def load_historical_snapshots(
    snapshots_dir: Path,
    market_context_dir: Optional[Path] = None,
    *,
    allow_unverified_historical: bool = False,
) -> List[HistoricalSnapshot]:
    if not snapshots_dir.exists():
        raise FileNotFoundError(snapshots_dir)
    snapshots: List[HistoricalSnapshot] = []
    for path in sorted(snapshots_dir.iterdir()):
        if not path.is_file() or not SNAPSHOT_FILENAME_RE.match(path.name):
            continue
        payload = _extract_snapshot_payload(path)
        as_of = _validate_snapshot_date(path, payload)
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        point_in_time_verified = bool(metadata.get("point_in_time_verified") is True)
        if not point_in_time_verified and not allow_unverified_historical:
            raise ValueError(
                f"Snapshot {path.name} is not point-in-time verified; rerun with --allow-unverified-historical to proceed"
            )
        records = load_records(path)
        market_context = None
        if market_context_dir is not None:
            sidecar_path = market_context_dir / f"{path.stem}.market-context.json"
            if sidecar_path.exists():
                market_context = load_market_context(sidecar_path)
        snapshots.append(
            HistoricalSnapshot(
                as_of=as_of,
                path=path,
                payload=payload,
                records=records,
                market_context=market_context,
                point_in_time_verified=point_in_time_verified,
            )
        )
    if not snapshots:
        raise ValueError(f"No dated snapshots found in {snapshots_dir}")
    return snapshots


def load_spy_prices(spy_prices_path: Optional[Path], snapshots: Sequence[HistoricalSnapshot]) -> Dict[str, float]:
    if spy_prices_path is not None:
        payload = _load_json(spy_prices_path)
        if isinstance(payload, dict):
            if "prices" in payload and isinstance(payload["prices"], dict):
                source = payload["prices"]
            else:
                source = payload
            result = {}
            for key, value in source.items():
                number = _safe_float(value)
                if number is not None:
                    result[str(key)] = number
            return result
        raise ValueError("spy_prices.json must be an object keyed by YYYY-MM-DD")

    result: Dict[str, float] = {}
    for snapshot in snapshots:
        metadata = snapshot.payload.get("metadata") if isinstance(snapshot.payload, dict) else None
        if not isinstance(metadata, dict):
            continue
        if isinstance(metadata.get("benchmarks"), dict):
            benchmarks = metadata["benchmarks"]
            if isinstance(benchmarks.get("SPY"), dict):
                price = _safe_float(benchmarks["SPY"].get("close"))
            else:
                price = _safe_float(benchmarks.get("SPY"))
            if price is not None:
                result[snapshot.as_of.isoformat()] = price
    return result


def _month_key(as_of: date) -> Tuple[int, int]:
    return as_of.year, as_of.month


def _quarter_key(as_of: date) -> Tuple[int, int]:
    quarter = ((as_of.month - 1) // 3) + 1
    return as_of.year, quarter


def select_rebalance_snapshots(snapshots: Sequence[HistoricalSnapshot], strategy_mode: str) -> List[HistoricalSnapshot]:
    grouped: Dict[Tuple[int, int], HistoricalSnapshot] = {}
    for snapshot in snapshots:
        key = _month_key(snapshot.as_of) if strategy_mode == "hybrid" else _quarter_key(snapshot.as_of)
        previous = grouped.get(key)
        if previous is None or snapshot.as_of > previous.as_of:
            grouped[key] = snapshot
    return [grouped[key] for key in sorted(grouped)]


def _configured_portfolios(strategy_mode: str, eligible_candidate_count: int) -> List[Tuple[str, int]]:
    if strategy_mode == "hybrid":
        decile_count = max(1, math.floor(eligible_candidate_count * 0.10))
        return [(TOP_20, 20), (TOP_50, 50), (TOP_DECILE, decile_count)]
    return [(TOP_20, 20), (TOP_50, 50)]


def _equal_weight_map(tickers: Sequence[str]) -> Dict[str, float]:
    if not tickers:
        return {}
    weight = 1.0 / len(tickers)
    return {ticker: weight for ticker in tickers}


def _turnover(previous_weights: Dict[str, float], current_weights: Dict[str, float]) -> Optional[float]:
    if not previous_weights:
        return None
    tickers = set(previous_weights) | set(current_weights)
    return 0.5 * sum(abs(current_weights.get(ticker, 0.0) - previous_weights.get(ticker, 0.0)) for ticker in tickers)


def _sector_exposure(records_by_ticker: Dict[str, Any], holdings: Sequence[str]) -> Dict[str, float]:
    exposures: Dict[str, float] = {}
    if not holdings:
        return exposures
    weight = 1.0 / len(holdings)
    for ticker in holdings:
        record = records_by_ticker.get(ticker)
        sector = getattr(record, "sector", None) or "unknown"
        exposures[sector] = exposures.get(sector, 0.0) + weight
    return {key: round(value, 6) for key, value in sorted(exposures.items())}


def _top_10_weight(holding_count: int) -> Optional[float]:
    if holding_count <= 0:
        return None
    return min(10, holding_count) / holding_count


def _formation_report(
    snapshot: HistoricalSnapshot,
    strategy_mode: str,
    config: ScreenConfig,
    min_score: Optional[float],
) -> Any:
    return build_report(
        snapshot.records,
        config=config,
        top_n=len(snapshot.records),
        strategy_mode=strategy_mode,
        as_of=snapshot.as_of,
        force_rebalance=(strategy_mode == "stop_checking_price"),
        min_score=min_score,
        market_context=snapshot.market_context,
    )


def _hard_pass_universe(snapshot: HistoricalSnapshot, config: ScreenConfig, strategy_mode: str) -> List[Any]:
    scored = [
        score_record(
            record,
            config,
            strategy_mode=strategy_mode,
            as_of=snapshot.as_of,
            force_rebalance=(strategy_mode == "stop_checking_price"),
        )
        for record in snapshot.records
    ]
    return [item.record for item in scored if item.is_candidate and item.record is not None]


def _price_map(records: Iterable[Any]) -> Dict[str, Optional[float]]:
    return {record.ticker: _safe_float(record.price) for record in records}


def _record_map(records: Iterable[Any]) -> Dict[str, Any]:
    return {record.ticker: record for record in records}


def _portfolio_return(
    holdings: Sequence[str],
    formation_prices: Dict[str, Optional[float]],
    next_prices: Dict[str, Optional[float]],
) -> Tuple[Optional[float], List[str], Dict[str, float]]:
    missing: List[str] = []
    holding_returns: Dict[str, float] = {}
    for ticker in holdings:
        start_price = formation_prices.get(ticker)
        end_price = next_prices.get(ticker)
        if start_price is None or end_price is None:
            missing.append(ticker)
            continue
        if start_price == 0:
            missing.append(ticker)
            continue
        holding_returns[ticker] = (end_price / start_price) - 1.0
    if missing:
        return None, sorted(set(missing)), {}
    if not holding_returns:
        return None, [], {}
    return mean(holding_returns.values()), [], holding_returns


def _spy_period_return(formation_date: date, next_date: date, spy_prices: Dict[str, float]) -> Tuple[Optional[float], Optional[str]]:
    start_price = spy_prices.get(formation_date.isoformat())
    end_price = spy_prices.get(next_date.isoformat())
    if start_price is None or end_price is None:
        return None, "missing_spy_price"
    if start_price == 0:
        return None, "invalid_spy_price"
    return (end_price / start_price) - 1.0, None


def _equal_weight_universe_return(
    hard_pass_records: Sequence[Any],
    next_prices: Dict[str, Optional[float]],
) -> Tuple[Optional[float], int]:
    valid_returns: List[float] = []
    for record in hard_pass_records:
        start_price = _safe_float(record.price)
        end_price = next_prices.get(record.ticker)
        if start_price is None or end_price is None or start_price == 0:
            continue
        valid_returns.append((end_price / start_price) - 1.0)
    if not valid_returns:
        return None, 0
    return mean(valid_returns), len(valid_returns)


def _cagr(period_returns: Sequence[float], annualization_factor: int) -> Optional[float]:
    if not period_returns:
        return None
    total = 1.0
    for value in period_returns:
        total *= 1.0 + value
    return total ** (annualization_factor / len(period_returns)) - 1.0


def _annualized_volatility(period_returns: Sequence[float], annualization_factor: int) -> Optional[float]:
    if len(period_returns) < 2:
        return None
    return stdev(period_returns) * math.sqrt(annualization_factor)


def _sharpe(period_returns: Sequence[float], annualization_factor: int) -> Optional[float]:
    volatility = _annualized_volatility(period_returns, annualization_factor)
    if volatility in (None, 0):
        return None
    return mean(period_returns) / stdev(period_returns) * math.sqrt(annualization_factor)


def _sortino(period_returns: Sequence[float], annualization_factor: int) -> Optional[float]:
    downside = [min(0.0, value) for value in period_returns]
    negative_periods = [value for value in period_returns if value < 0]
    if not period_returns or not negative_periods:
        return None
    downside_deviation = math.sqrt(sum(value * value for value in downside) / len(downside))
    if downside_deviation == 0:
        return None
    return mean(period_returns) / downside_deviation * math.sqrt(annualization_factor)


def _sortino_reason(period_returns: Sequence[float]) -> str:
    downside = [value for value in period_returns if value < 0]
    if not period_returns or not downside:
        return "no_negative_return_periods"
    return ""


def _coverage_ratio(valid_count: int, expected_count: int) -> float:
    if expected_count <= 0:
        return 0.0
    return valid_count / expected_count


def _metric_status(valid_count: int, expected_count: int) -> str:
    return "incomplete_coverage" if _coverage_ratio(valid_count, expected_count) < 0.90 else "ok"


def _max_drawdown(period_returns: Sequence[float]) -> Optional[float]:
    if not period_returns:
        return None
    peak = 1.0
    equity = 1.0
    max_drawdown = 0.0
    for value in period_returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        drawdown = (equity / peak) - 1.0
        max_drawdown = min(max_drawdown, drawdown)
    return max_drawdown


def _average_holding_period(periods: Sequence[PortfolioPeriod]) -> Optional[float]:
    streaks: Dict[str, int] = {}
    completed: List[int] = []
    previous_holdings: set[str] = set()
    for period in periods:
        holdings = set(period.holdings)
        continuing = holdings & previous_holdings
        ended = previous_holdings - holdings
        started = holdings - previous_holdings
        for ticker in ended:
            completed.append(streaks.pop(ticker))
        for ticker in continuing:
            streaks[ticker] += 1
        for ticker in started:
            streaks[ticker] = 1
        previous_holdings = holdings
    completed.extend(streaks.values())
    if not completed:
        return None
    return mean(completed)


def _average_sector_exposure(periods: Sequence[PortfolioPeriod]) -> Dict[str, float]:
    if not periods:
        return {}
    totals: Dict[str, float] = {}
    for period in periods:
        for sector, weight in period.sector_exposure.items():
            totals[sector] = totals.get(sector, 0.0) + weight
    return {sector: round(weight / len(periods), 6) for sector, weight in sorted(totals.items())}


def _summarize_periods(
    strategy_mode: str,
    portfolio_name: str,
    periods: Sequence[PortfolioPeriod],
) -> BacktestSummary:
    annualization_factor = 12 if strategy_mode == "hybrid" else 4
    expected_period_count = len(periods)
    valid_periods = [period for period in periods if period.return_value is not None]
    valid_returns = [period.return_value for period in valid_periods if period.return_value is not None]
    spy_valid = [period.benchmark_spy_return for period in periods if period.benchmark_spy_return is not None]
    universe_valid = [period.benchmark_universe_return for period in periods if period.benchmark_universe_return is not None]
    missing_tickers = sorted({ticker for period in periods for ticker in period.missing_return_tickers})
    turnover_values = [period.turnover for period in periods if period.turnover is not None]
    positive_holding_returns = 0
    total_holding_returns = 0
    for period in valid_periods:
        for value in period.holding_returns.values():
            total_holding_returns += 1
            if value > 0:
                positive_holding_returns += 1
    concentration_values = [period.top_10_weight for period in periods if period.top_10_weight is not None]
    portfolio_metric_status = _metric_status(len(valid_periods), expected_period_count)
    benchmark_spy_expected_period_count = expected_period_count
    benchmark_spy_valid_period_count = len(spy_valid)
    benchmark_spy_metric_status = _metric_status(benchmark_spy_valid_period_count, benchmark_spy_expected_period_count)
    benchmark_universe_expected_period_count = expected_period_count
    benchmark_universe_valid_period_count = len(universe_valid)
    benchmark_universe_metric_status = _metric_status(benchmark_universe_valid_period_count, benchmark_universe_expected_period_count)
    return BacktestSummary(
        strategy_mode=strategy_mode,
        portfolio_name=portfolio_name,
        rebalance_frequency="monthly" if strategy_mode == "hybrid" else "quarterly",
        periods=list(periods),
        annualization_factor=annualization_factor,
        missing_return_period_count=sum(1 for period in periods if period.return_value is None),
        missing_return_ticker_count=len(missing_tickers),
        missing_return_tickers=missing_tickers,
        total_formations=len(periods),
        expected_period_count=expected_period_count,
        valid_period_count=len(valid_periods),
        invalid_period_count=expected_period_count - len(valid_periods),
        no_candidate_period_count=sum(1 for period in periods if period.period_status == "no_candidates"),
        coverage_ratio=_coverage_ratio(len(valid_periods), expected_period_count),
        metric_status=portfolio_metric_status,
        cagr=None if portfolio_metric_status == "incomplete_coverage" else _cagr(valid_returns, annualization_factor),
        annualized_volatility=_annualized_volatility(valid_returns, annualization_factor),
        sharpe_ratio=None if portfolio_metric_status == "incomplete_coverage" else _sharpe(valid_returns, annualization_factor),
        sortino_ratio=None if portfolio_metric_status == "incomplete_coverage" else _sortino(valid_returns, annualization_factor),
        sortino_reason=("incomplete_coverage" if portfolio_metric_status == "incomplete_coverage" else _sortino_reason(valid_returns)),
        max_drawdown=None if portfolio_metric_status == "incomplete_coverage" else _max_drawdown(valid_returns),
        average_turnover=mean(turnover_values) if turnover_values else None,
        hit_rate=(positive_holding_returns / total_holding_returns) if total_holding_returns else None,
        average_holding_period=_average_holding_period(periods),
        average_sector_exposure=_average_sector_exposure(periods),
        average_top_holdings_concentration=(mean(concentration_values) if concentration_values else None),
        benchmark_spy_expected_period_count=benchmark_spy_expected_period_count,
        benchmark_spy_valid_period_count=benchmark_spy_valid_period_count,
        benchmark_spy_coverage_ratio=_coverage_ratio(benchmark_spy_valid_period_count, benchmark_spy_expected_period_count),
        benchmark_spy_metric_status=benchmark_spy_metric_status,
        benchmark_spy_cagr=None if benchmark_spy_metric_status == "incomplete_coverage" else _cagr(spy_valid, annualization_factor),
        benchmark_spy_annualized_volatility=_annualized_volatility(spy_valid, annualization_factor),
        benchmark_spy_sharpe_ratio=None if benchmark_spy_metric_status == "incomplete_coverage" else _sharpe(spy_valid, annualization_factor),
        benchmark_spy_sortino_ratio=None if benchmark_spy_metric_status == "incomplete_coverage" else _sortino(spy_valid, annualization_factor),
        benchmark_spy_sortino_reason=("incomplete_coverage" if benchmark_spy_metric_status == "incomplete_coverage" else _sortino_reason(spy_valid)),
        benchmark_spy_max_drawdown=None if benchmark_spy_metric_status == "incomplete_coverage" else _max_drawdown(spy_valid),
        benchmark_universe_expected_period_count=benchmark_universe_expected_period_count,
        benchmark_universe_valid_period_count=benchmark_universe_valid_period_count,
        benchmark_universe_coverage_ratio=_coverage_ratio(benchmark_universe_valid_period_count, benchmark_universe_expected_period_count),
        benchmark_universe_metric_status=benchmark_universe_metric_status,
        benchmark_universe_cagr=None if benchmark_universe_metric_status == "incomplete_coverage" else _cagr(universe_valid, annualization_factor),
        benchmark_universe_annualized_volatility=_annualized_volatility(universe_valid, annualization_factor),
        benchmark_universe_sharpe_ratio=None if benchmark_universe_metric_status == "incomplete_coverage" else _sharpe(universe_valid, annualization_factor),
        benchmark_universe_sortino_ratio=None if benchmark_universe_metric_status == "incomplete_coverage" else _sortino(universe_valid, annualization_factor),
        benchmark_universe_sortino_reason=("incomplete_coverage" if benchmark_universe_metric_status == "incomplete_coverage" else _sortino_reason(universe_valid)),
        benchmark_universe_max_drawdown=None if benchmark_universe_metric_status == "incomplete_coverage" else _max_drawdown(universe_valid),
    )


def run_backtest(
    snapshots: Sequence[HistoricalSnapshot],
    strategy_mode: str,
    config: Optional[ScreenConfig] = None,
    min_score: Optional[float] = None,
    spy_prices: Optional[Dict[str, float]] = None,
) -> List[BacktestSummary]:
    config = config or ScreenConfig()
    rebalance_snapshots = select_rebalance_snapshots(snapshots, strategy_mode)
    if len(rebalance_snapshots) < 2:
        raise ValueError("At least two rebalance snapshots are required for backtesting")
    spy_prices = spy_prices or {}
    periods_by_portfolio: Dict[str, List[PortfolioPeriod]] = {}
    previous_weights: Dict[str, Dict[str, float]] = {}

    for formation, next_snapshot in zip(rebalance_snapshots, rebalance_snapshots[1:]):
        report = _formation_report(formation, strategy_mode, config, min_score)
        official_candidates = list(report.candidates)
        candidate_count = len(official_candidates)
        formation_prices = _price_map(formation.records)
        next_prices = _price_map(next_snapshot.records)
        formation_record_map = _record_map(formation.records)
        hard_pass_records = _hard_pass_universe(formation, config, strategy_mode)
        universe_return, universe_eligible_count = _equal_weight_universe_return(hard_pass_records, next_prices)
        spy_return, spy_reason = _spy_period_return(formation.as_of, next_snapshot.as_of, spy_prices)

        for portfolio_name, requested_size in _configured_portfolios(strategy_mode, candidate_count):
            holding_count = min(requested_size, candidate_count)
            holdings = [item.ticker for item in official_candidates[:holding_count]] if holding_count > 0 else []
            weights = _equal_weight_map(holdings)
            return_value, missing_tickers, holding_returns = _portfolio_return(holdings, formation_prices, next_prices)
            period_status = "valid"
            if holding_count <= 0:
                period_status = "no_candidates"
            elif return_value is None:
                period_status = "missing_next_period_price"
            period = PortfolioPeriod(
                formation_date=formation.as_of,
                next_date=next_snapshot.as_of,
                portfolio_name=portfolio_name,
                holdings=holdings,
                holding_count=len(holdings),
                return_value=return_value,
                benchmark_spy_return=spy_return,
                benchmark_universe_return=universe_return,
                turnover=_turnover(previous_weights.get(portfolio_name, {}), weights),
                top_10_weight=_top_10_weight(len(holdings)),
                sector_exposure=_sector_exposure(formation_record_map, holdings),
                missing_return_tickers=missing_tickers,
                period_status=period_status,
                benchmark_missing_reason=spy_reason,
                universe_benchmark_eligible_count=universe_eligible_count,
                benchmark_spy_status="valid" if spy_return is not None else (spy_reason or "missing_spy_price"),
                benchmark_universe_status="valid" if universe_return is not None else "missing_next_period_price",
                holding_returns=holding_returns,
            )
            periods_by_portfolio.setdefault(portfolio_name, []).append(period)
            previous_weights[portfolio_name] = weights

    return [
        _summarize_periods(strategy_mode, portfolio_name, periods)
        for portfolio_name, periods in sorted(periods_by_portfolio.items())
    ]


def _serialize_flags() -> Dict[str, Any]:
    return dict(RESEARCH_FLAGS)


def _metric_text(value: Optional[float], status: str) -> str:
    if status == "incomplete_coverage":
        return "INCOMPLETE"
    if value is None:
        return "N/A"
    rounded = _safe_round(value)
    return str(rounded)


def _summary_rows(summaries: Sequence[BacktestSummary]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    flags = _serialize_flags()
    for summary in summaries:
        row = {
            "strategy_mode": summary.strategy_mode,
            "portfolio_name": summary.portfolio_name,
            "rebalance_frequency": summary.rebalance_frequency,
            "annualization_factor": summary.annualization_factor,
            "total_formations": summary.total_formations,
            "expected_period_count": summary.expected_period_count,
            "valid_period_count": summary.valid_period_count,
            "invalid_period_count": summary.invalid_period_count,
            "no_candidate_period_count": summary.no_candidate_period_count,
            "coverage_ratio": _safe_round(summary.coverage_ratio),
            "metric_status": summary.metric_status,
            "missing_return_period_count": summary.missing_return_period_count,
            "missing_return_ticker_count": summary.missing_return_ticker_count,
            "missing_return_tickers": json.dumps(summary.missing_return_tickers, ensure_ascii=False),
            "cagr": _metric_text(summary.cagr, summary.metric_status),
            "annualized_volatility": _safe_round(summary.annualized_volatility),
            "sharpe_ratio": _metric_text(summary.sharpe_ratio, summary.metric_status),
            "sortino_ratio": (
                "INCOMPLETE"
                if summary.sortino_reason == "incomplete_coverage"
                else (_safe_round(summary.sortino_ratio) if summary.sortino_ratio is not None else "N/A")
            ),
            "sortino_reason": summary.sortino_reason,
            "max_drawdown": _metric_text(summary.max_drawdown, summary.metric_status),
            "average_turnover": _safe_round(summary.average_turnover),
            "hit_rate": _safe_round(summary.hit_rate),
            "average_holding_period": _safe_round(summary.average_holding_period),
            "sector_exposure": json.dumps(summary.average_sector_exposure, ensure_ascii=False, sort_keys=True),
            "top_holdings_concentration": _safe_round(summary.average_top_holdings_concentration),
            "benchmark_spy_expected_period_count": summary.benchmark_spy_expected_period_count,
            "benchmark_spy_valid_period_count": summary.benchmark_spy_valid_period_count,
            "benchmark_spy_coverage_ratio": _safe_round(summary.benchmark_spy_coverage_ratio),
            "benchmark_spy_metric_status": summary.benchmark_spy_metric_status,
            "benchmark_spy_cagr": _metric_text(summary.benchmark_spy_cagr, summary.benchmark_spy_metric_status),
            "benchmark_spy_annualized_volatility": _safe_round(summary.benchmark_spy_annualized_volatility),
            "benchmark_spy_sharpe_ratio": _metric_text(summary.benchmark_spy_sharpe_ratio, summary.benchmark_spy_metric_status),
            "benchmark_spy_sortino_ratio": (
                "INCOMPLETE"
                if summary.benchmark_spy_sortino_reason == "incomplete_coverage"
                else (_safe_round(summary.benchmark_spy_sortino_ratio) if summary.benchmark_spy_sortino_ratio is not None else "N/A")
            ),
            "benchmark_spy_sortino_reason": summary.benchmark_spy_sortino_reason,
            "benchmark_spy_max_drawdown": _metric_text(summary.benchmark_spy_max_drawdown, summary.benchmark_spy_metric_status),
            "benchmark_universe_expected_period_count": summary.benchmark_universe_expected_period_count,
            "benchmark_universe_valid_period_count": summary.benchmark_universe_valid_period_count,
            "benchmark_universe_coverage_ratio": _safe_round(summary.benchmark_universe_coverage_ratio),
            "benchmark_universe_metric_status": summary.benchmark_universe_metric_status,
            "benchmark_universe_cagr": _metric_text(summary.benchmark_universe_cagr, summary.benchmark_universe_metric_status),
            "benchmark_universe_annualized_volatility": _safe_round(summary.benchmark_universe_annualized_volatility),
            "benchmark_universe_sharpe_ratio": _metric_text(summary.benchmark_universe_sharpe_ratio, summary.benchmark_universe_metric_status),
            "benchmark_universe_sortino_ratio": (
                "INCOMPLETE"
                if summary.benchmark_universe_sortino_reason == "incomplete_coverage"
                else (_safe_round(summary.benchmark_universe_sortino_ratio) if summary.benchmark_universe_sortino_ratio is not None else "N/A")
            ),
            "benchmark_universe_sortino_reason": summary.benchmark_universe_sortino_reason,
            "benchmark_universe_max_drawdown": _metric_text(summary.benchmark_universe_max_drawdown, summary.benchmark_universe_metric_status),
        }
        row.update(flags)
        rows.append(row)
    return rows


def _period_rows(summaries: Sequence[BacktestSummary]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    flags = _serialize_flags()
    for summary in summaries:
        for period in summary.periods:
            row = {
                "strategy_mode": summary.strategy_mode,
                "portfolio_name": summary.portfolio_name,
                "formation_date": period.formation_date.isoformat(),
                "next_date": period.next_date.isoformat(),
                "holding_count": period.holding_count,
                "holdings": json.dumps(period.holdings, ensure_ascii=False),
                "portfolio_return": _safe_round(period.return_value),
                "period_status": period.period_status,
                "benchmark_spy_return": _safe_round(period.benchmark_spy_return),
                "benchmark_spy_status": period.benchmark_spy_status,
                "benchmark_universe_return": _safe_round(period.benchmark_universe_return),
                "benchmark_universe_status": period.benchmark_universe_status,
                "turnover": _safe_round(period.turnover),
                "top_10_weight": _safe_round(period.top_10_weight),
                "sector_exposure": json.dumps(period.sector_exposure, ensure_ascii=False, sort_keys=True),
                "missing_return_tickers": json.dumps(period.missing_return_tickers, ensure_ascii=False),
                "benchmark_missing_reason": period.benchmark_missing_reason,
                "universe_benchmark_eligible_count": period.universe_benchmark_eligible_count,
            }
            row.update(flags)
            rows.append(row)
    return rows


def write_csv(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def render_markdown_report(
    summaries: Sequence[BacktestSummary],
    strategy_mode: str,
    snapshots: Sequence[HistoricalSnapshot],
) -> str:
    lines = ["# Backtest Report", ""]
    lines.append(f"- strategy_mode: `{strategy_mode}`")
    lines.append(f"- snapshots_loaded: {len(snapshots)}")
    lines.append(f"- first_snapshot: {snapshots[0].as_of.isoformat()}")
    lines.append(f"- last_snapshot: {snapshots[-1].as_of.isoformat()}")
    for key, value in RESEARCH_FLAGS.items():
        lines.append(f"- {key}: `{str(value).lower() if isinstance(value, bool) else value}`")
    lines.append("")
    for summary in summaries:
        lines.append(f"## {summary.portfolio_name}")
        lines.append("")
        lines.append(f"- rebalance_frequency: {summary.rebalance_frequency}")
        lines.append(f"- annualization_factor: {summary.annualization_factor}")
        lines.append(f"- total_formations: {summary.total_formations}")
        lines.append(f"- expected_period_count: {summary.expected_period_count}")
        lines.append(f"- valid_period_count: {summary.valid_period_count}")
        lines.append(f"- invalid_period_count: {summary.invalid_period_count}")
        lines.append(f"- no_candidate_period_count: {summary.no_candidate_period_count}")
        lines.append(f"- coverage_ratio: {_safe_round(summary.coverage_ratio)}")
        lines.append(f"- metric_status: {summary.metric_status}")
        lines.append(f"- missing_return_period_count: {summary.missing_return_period_count}")
        lines.append(f"- missing_return_ticker_count: {summary.missing_return_ticker_count}")
        lines.append(f"- missing_return_tickers: {', '.join(summary.missing_return_tickers) if summary.missing_return_tickers else 'none'}")
        lines.append(f"- CAGR: {_metric_text(summary.cagr, summary.metric_status)}")
        lines.append(f"- annualized_volatility: {_safe_round(summary.annualized_volatility)}")
        lines.append(f"- Sharpe: {_metric_text(summary.sharpe_ratio, summary.metric_status)}")
        lines.append(f"- Sortino: {'INCOMPLETE' if summary.sortino_reason == 'incomplete_coverage' else (_safe_round(summary.sortino_ratio) if summary.sortino_ratio is not None else 'N/A')}")
        lines.append(f"- Sortino reason: {summary.sortino_reason.replace('_', ' ') if summary.sortino_reason else ''}")
        lines.append(f"- max_drawdown: {_metric_text(summary.max_drawdown, summary.metric_status)}")
        lines.append(f"- average_turnover: {_safe_round(summary.average_turnover)}")
        lines.append(f"- hit_rate: {_safe_round(summary.hit_rate)}")
        lines.append(f"- average_holding_period: {_safe_round(summary.average_holding_period)}")
        lines.append(f"- sector_exposure: `{json.dumps(summary.average_sector_exposure, ensure_ascii=False, sort_keys=True)}`")
        lines.append(f"- top_holdings_concentration: {_safe_round(summary.average_top_holdings_concentration)}")
        lines.append(f"- benchmark_spy_expected_period_count: {summary.benchmark_spy_expected_period_count}")
        lines.append(f"- benchmark_spy_valid_period_count: {summary.benchmark_spy_valid_period_count}")
        lines.append(f"- benchmark_spy_coverage_ratio: {_safe_round(summary.benchmark_spy_coverage_ratio)}")
        lines.append(f"- benchmark_spy_metric_status: {summary.benchmark_spy_metric_status}")
        lines.append(f"- benchmark_spy_cagr: {_metric_text(summary.benchmark_spy_cagr, summary.benchmark_spy_metric_status)}")
        lines.append(f"- benchmark_spy_sortino: {'INCOMPLETE' if summary.benchmark_spy_sortino_reason == 'incomplete_coverage' else (_safe_round(summary.benchmark_spy_sortino_ratio) if summary.benchmark_spy_sortino_ratio is not None else 'N/A')}")
        lines.append(f"- benchmark_spy_sortino_reason: {summary.benchmark_spy_sortino_reason.replace('_', ' ') if summary.benchmark_spy_sortino_reason else ''}")
        lines.append(f"- benchmark_universe_expected_period_count: {summary.benchmark_universe_expected_period_count}")
        lines.append(f"- benchmark_universe_valid_period_count: {summary.benchmark_universe_valid_period_count}")
        lines.append(f"- benchmark_universe_coverage_ratio: {_safe_round(summary.benchmark_universe_coverage_ratio)}")
        lines.append(f"- benchmark_universe_metric_status: {summary.benchmark_universe_metric_status}")
        lines.append(f"- benchmark_universe_cagr: {_metric_text(summary.benchmark_universe_cagr, summary.benchmark_universe_metric_status)}")
        lines.append(f"- benchmark_universe_sortino: {'INCOMPLETE' if summary.benchmark_universe_sortino_reason == 'incomplete_coverage' else (_safe_round(summary.benchmark_universe_sortino_ratio) if summary.benchmark_universe_sortino_ratio is not None else 'N/A')}")
        lines.append(f"- benchmark_universe_sortino_reason: {summary.benchmark_universe_sortino_reason.replace('_', ' ') if summary.benchmark_universe_sortino_reason else ''}")
        lines.append("")
    return "\n".join(lines) + "\n"


def write_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _load_config(path: Optional[Path]) -> ScreenConfig:
    if path is None:
        return ScreenConfig()
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise ValueError("Config must be a JSON object")
    return ScreenConfig.from_dict(payload)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run research-only backtests for the US stock screener.")
    parser.add_argument("--snapshots-dir", required=True, help="Directory of dated YYYY-MM-DD.json snapshots")
    parser.add_argument("--strategy-mode", choices=("hybrid", "stop_checking_price"), required=True)
    parser.add_argument("--output-prefix", required=True, help="Output path prefix for CSV and Markdown reports")
    parser.add_argument("--spy-prices", help="Optional local spy_prices.json benchmark file")
    parser.add_argument("--market-context-dir", help="Optional local directory containing YYYY-MM-DD.market-context.json files")
    parser.add_argument("--config", help="Optional screener config JSON")
    parser.add_argument("--min-score", type=float, help="Optional minimum score applied during ranking")
    parser.add_argument("--allow-unverified-historical", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    snapshots_dir = Path(args.snapshots_dir)
    market_context_dir = Path(args.market_context_dir) if args.market_context_dir else None
    config = _load_config(Path(args.config) if args.config else None)
    previous_allow_unverified = RESEARCH_FLAGS["allow_unverified_historical"]
    RESEARCH_FLAGS["allow_unverified_historical"] = bool(args.allow_unverified_historical)
    try:
        snapshots = load_historical_snapshots(
            snapshots_dir,
            market_context_dir=market_context_dir,
            allow_unverified_historical=bool(args.allow_unverified_historical),
        )
        spy_prices = load_spy_prices(Path(args.spy_prices) if args.spy_prices else None, snapshots)
        summaries = run_backtest(
            snapshots,
            args.strategy_mode,
            config=config,
            min_score=args.min_score,
            spy_prices=spy_prices,
        )
        output_prefix = Path(args.output_prefix)
        write_csv(output_prefix.with_suffix(".summary.csv"), _summary_rows(summaries))
        write_csv(output_prefix.with_suffix(".periods.csv"), _period_rows(summaries))
        write_markdown(
            output_prefix.with_suffix(".md"),
            render_markdown_report(summaries, args.strategy_mode, snapshots),
        )
        return 0
    finally:
        RESEARCH_FLAGS["allow_unverified_historical"] = previous_allow_unverified


if __name__ == "__main__":
    raise SystemExit(main())
