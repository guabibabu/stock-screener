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
    benchmark_missing_reason: Optional[str] = None
    universe_benchmark_eligible_count: int = 0
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
    valid_period_count: int
    cagr: Optional[float]
    annualized_volatility: Optional[float]
    sharpe_ratio: Optional[float]
    sortino_ratio: Optional[float]
    max_drawdown: Optional[float]
    average_turnover: Optional[float]
    hit_rate: Optional[float]
    average_holding_period: Optional[float]
    average_sector_exposure: Dict[str, float]
    average_top_holdings_concentration: Optional[float]
    benchmark_spy_cagr: Optional[float]
    benchmark_spy_annualized_volatility: Optional[float]
    benchmark_spy_sharpe_ratio: Optional[float]
    benchmark_spy_sortino_ratio: Optional[float]
    benchmark_spy_max_drawdown: Optional[float]
    benchmark_universe_cagr: Optional[float]
    benchmark_universe_annualized_volatility: Optional[float]
    benchmark_universe_sharpe_ratio: Optional[float]
    benchmark_universe_sortino_ratio: Optional[float]
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
    if isinstance(metadata, dict) and metadata.get("as_of"):
        payload_date = date.fromisoformat(str(metadata["as_of"]))
        if payload_date != filename_date:
            raise ValueError(
                f"Snapshot {path.name} has metadata.as_of={payload_date.isoformat()} "
                f"which does not match filename date {filename_date.isoformat()}"
            )
    return filename_date


def load_historical_snapshots(
    snapshots_dir: Path,
    market_context_dir: Optional[Path] = None,
) -> List[HistoricalSnapshot]:
    if not snapshots_dir.exists():
        raise FileNotFoundError(snapshots_dir)
    snapshots: List[HistoricalSnapshot] = []
    for path in sorted(snapshots_dir.iterdir()):
        if not path.is_file() or not SNAPSHOT_FILENAME_RE.match(path.name):
            continue
        payload = _extract_snapshot_payload(path)
        as_of = _validate_snapshot_date(path, payload)
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
    downside = [value for value in period_returns if value < 0]
    if not period_returns or not downside:
        return None
    if len(downside) == 1:
        downside_deviation = abs(downside[0])
    else:
        downside_deviation = stdev(downside)
    if downside_deviation == 0:
        return None
    return mean(period_returns) / downside_deviation * math.sqrt(annualization_factor)


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
        valid_period_count=len(valid_periods),
        cagr=_cagr(valid_returns, annualization_factor),
        annualized_volatility=_annualized_volatility(valid_returns, annualization_factor),
        sharpe_ratio=_sharpe(valid_returns, annualization_factor),
        sortino_ratio=_sortino(valid_returns, annualization_factor),
        max_drawdown=_max_drawdown(valid_returns),
        average_turnover=mean(turnover_values) if turnover_values else None,
        hit_rate=(positive_holding_returns / total_holding_returns) if total_holding_returns else None,
        average_holding_period=_average_holding_period(periods),
        average_sector_exposure=_average_sector_exposure(periods),
        average_top_holdings_concentration=(mean(concentration_values) if concentration_values else None),
        benchmark_spy_cagr=_cagr(spy_valid, annualization_factor),
        benchmark_spy_annualized_volatility=_annualized_volatility(spy_valid, annualization_factor),
        benchmark_spy_sharpe_ratio=_sharpe(spy_valid, annualization_factor),
        benchmark_spy_sortino_ratio=_sortino(spy_valid, annualization_factor),
        benchmark_spy_max_drawdown=_max_drawdown(spy_valid),
        benchmark_universe_cagr=_cagr(universe_valid, annualization_factor),
        benchmark_universe_annualized_volatility=_annualized_volatility(universe_valid, annualization_factor),
        benchmark_universe_sharpe_ratio=_sharpe(universe_valid, annualization_factor),
        benchmark_universe_sortino_ratio=_sortino(universe_valid, annualization_factor),
        benchmark_universe_max_drawdown=_max_drawdown(universe_valid),
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
        if candidate_count == 0:
            continue
        formation_prices = _price_map(formation.records)
        next_prices = _price_map(next_snapshot.records)
        formation_record_map = _record_map(formation.records)
        hard_pass_records = _hard_pass_universe(formation, config, strategy_mode)
        universe_return, universe_eligible_count = _equal_weight_universe_return(hard_pass_records, next_prices)
        spy_return, spy_reason = _spy_period_return(formation.as_of, next_snapshot.as_of, spy_prices)

        for portfolio_name, requested_size in _configured_portfolios(strategy_mode, candidate_count):
            holding_count = min(requested_size, candidate_count)
            if holding_count <= 0:
                continue
            holdings = [item.ticker for item in official_candidates[:holding_count]]
            weights = _equal_weight_map(holdings)
            return_value, missing_tickers, holding_returns = _portfolio_return(holdings, formation_prices, next_prices)
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
                benchmark_missing_reason=spy_reason,
                universe_benchmark_eligible_count=universe_eligible_count,
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
            "valid_period_count": summary.valid_period_count,
            "missing_return_period_count": summary.missing_return_period_count,
            "missing_return_ticker_count": summary.missing_return_ticker_count,
            "missing_return_tickers": json.dumps(summary.missing_return_tickers, ensure_ascii=False),
            "cagr": _safe_round(summary.cagr),
            "annualized_volatility": _safe_round(summary.annualized_volatility),
            "sharpe_ratio": _safe_round(summary.sharpe_ratio),
            "sortino_ratio": _safe_round(summary.sortino_ratio),
            "max_drawdown": _safe_round(summary.max_drawdown),
            "average_turnover": _safe_round(summary.average_turnover),
            "hit_rate": _safe_round(summary.hit_rate),
            "average_holding_period": _safe_round(summary.average_holding_period),
            "sector_exposure": json.dumps(summary.average_sector_exposure, ensure_ascii=False, sort_keys=True),
            "top_holdings_concentration": _safe_round(summary.average_top_holdings_concentration),
            "benchmark_spy_cagr": _safe_round(summary.benchmark_spy_cagr),
            "benchmark_spy_annualized_volatility": _safe_round(summary.benchmark_spy_annualized_volatility),
            "benchmark_spy_sharpe_ratio": _safe_round(summary.benchmark_spy_sharpe_ratio),
            "benchmark_spy_sortino_ratio": _safe_round(summary.benchmark_spy_sortino_ratio),
            "benchmark_spy_max_drawdown": _safe_round(summary.benchmark_spy_max_drawdown),
            "benchmark_universe_cagr": _safe_round(summary.benchmark_universe_cagr),
            "benchmark_universe_annualized_volatility": _safe_round(summary.benchmark_universe_annualized_volatility),
            "benchmark_universe_sharpe_ratio": _safe_round(summary.benchmark_universe_sharpe_ratio),
            "benchmark_universe_sortino_ratio": _safe_round(summary.benchmark_universe_sortino_ratio),
            "benchmark_universe_max_drawdown": _safe_round(summary.benchmark_universe_max_drawdown),
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
                "benchmark_spy_return": _safe_round(period.benchmark_spy_return),
                "benchmark_universe_return": _safe_round(period.benchmark_universe_return),
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
        lines.append(f"- valid_period_count: {summary.valid_period_count}")
        lines.append(f"- missing_return_period_count: {summary.missing_return_period_count}")
        lines.append(f"- missing_return_ticker_count: {summary.missing_return_ticker_count}")
        lines.append(f"- missing_return_tickers: {', '.join(summary.missing_return_tickers) if summary.missing_return_tickers else 'none'}")
        lines.append(f"- CAGR: {_safe_round(summary.cagr)}")
        lines.append(f"- annualized_volatility: {_safe_round(summary.annualized_volatility)}")
        lines.append(f"- Sharpe: {_safe_round(summary.sharpe_ratio)}")
        lines.append(f"- Sortino: {_safe_round(summary.sortino_ratio)}")
        lines.append(f"- max_drawdown: {_safe_round(summary.max_drawdown)}")
        lines.append(f"- average_turnover: {_safe_round(summary.average_turnover)}")
        lines.append(f"- hit_rate: {_safe_round(summary.hit_rate)}")
        lines.append(f"- average_holding_period: {_safe_round(summary.average_holding_period)}")
        lines.append(f"- sector_exposure: `{json.dumps(summary.average_sector_exposure, ensure_ascii=False, sort_keys=True)}`")
        lines.append(f"- top_holdings_concentration: {_safe_round(summary.average_top_holdings_concentration)}")
        lines.append(f"- benchmark_spy_cagr: {_safe_round(summary.benchmark_spy_cagr)}")
        lines.append(f"- benchmark_universe_cagr: {_safe_round(summary.benchmark_universe_cagr)}")
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
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    snapshots_dir = Path(args.snapshots_dir)
    market_context_dir = Path(args.market_context_dir) if args.market_context_dir else None
    config = _load_config(Path(args.config) if args.config else None)
    snapshots = load_historical_snapshots(snapshots_dir, market_context_dir=market_context_dir)
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


if __name__ == "__main__":
    raise SystemExit(main())
