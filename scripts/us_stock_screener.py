#!/usr/bin/env python3
"""US stock screener with hybrid fundamental, momentum, and risk scoring."""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered in {"na", "n/a", "null", "none", "nan"}:
        return None
    text = text.replace(",", "").replace("%", "")
    try:
        return float(text)
    except ValueError:
        return None


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in {"1", "true", "t", "yes", "y", "halted", "otc", "etf", "adr"}


def _coerce_int(value: Any) -> Optional[int]:
    number = _coerce_float(value)
    if number is None:
        return None
    return int(number)


def _normalize_ticker(value: Any) -> str:
    return str(value).strip().upper()


def _pick(data: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] not in ("", None):
            return data[key]
    return None


def _clean_metadata_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _average(values: Sequence[Tuple[float, float]]) -> Optional[float]:
    total_weight = 0.0
    total_score = 0.0
    for score, weight in values:
        if weight <= 0:
            continue
        total_weight += weight
        total_score += score * weight
    if total_weight <= 0:
        return None
    return total_score / total_weight


def _compact(values: Sequence[Optional[Tuple[float, float]]]) -> List[Tuple[float, float]]:
    return [item for item in values if item is not None]


def _linear_score(value: Optional[float], lower: float, upper: float, *, higher_is_better: bool = True) -> Optional[float]:
    if value is None:
        return None
    if lower == upper:
        return 100.0 if value >= upper else 0.0
    clipped = max(min(value, upper), lower)
    if higher_is_better:
        ratio = (clipped - lower) / (upper - lower)
    else:
        ratio = (upper - clipped) / (upper - lower)
    return max(0.0, min(100.0, ratio * 100.0))


def _symmetric_score(value: Optional[float], negative_bound: float, positive_bound: float) -> Optional[float]:
    if value is None:
        return None
    if negative_bound >= 0 or positive_bound <= 0:
        raise ValueError("bounds must straddle zero")
    if value <= negative_bound:
        return 0.0
    if value >= positive_bound:
        return 100.0
    if value < 0:
        return (value - negative_bound) / (0 - negative_bound) * 50.0
    return 50.0 + (value / positive_bound) * 50.0


def _safe_round(value: Optional[float], digits: int = 1) -> Optional[float]:
    if value is None:
        return None
    return round(value, digits)


def _valid_number(value: Any) -> Optional[float]:
    number = _coerce_float(value)
    if number is None or math.isnan(number) or math.isinf(number):
        return None
    return number


def _valid_numbers(values: Iterable[Any]) -> List[float]:
    return [number for value in values if (number := _valid_number(value)) is not None]


def winsorize_value(value: Any, lower: float, upper: float) -> Optional[float]:
    if lower > upper:
        raise ValueError("lower must be <= upper")
    number = _valid_number(value)
    if number is None:
        return None
    return max(lower, min(upper, number))


def _quantile(sorted_values: Sequence[float], percentile: float) -> Optional[float]:
    if not sorted_values:
        return None
    if percentile < 0 or percentile > 1:
        raise ValueError("percentile must be between 0 and 1")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * percentile
    lower_index = int(math.floor(position))
    upper_index = int(math.ceil(position))
    if lower_index == upper_index:
        return sorted_values[lower_index]
    lower_value = sorted_values[lower_index]
    upper_value = sorted_values[upper_index]
    fraction = position - lower_index
    return lower_value + (upper_value - lower_value) * fraction


def winsorize_series(values: Sequence[Any], lower_pct: float = 0.05, upper_pct: float = 0.95) -> List[Optional[float]]:
    if lower_pct < 0 or upper_pct > 1 or lower_pct > upper_pct:
        raise ValueError("percentile bounds must satisfy 0 <= lower_pct <= upper_pct <= 1")
    valid = sorted(_valid_numbers(values))
    if not valid:
        return [None for _ in values]
    lower = _quantile(valid, lower_pct)
    upper = _quantile(valid, upper_pct)
    if lower is None or upper is None:
        return [None for _ in values]
    return [winsorize_value(value, lower, upper) for value in values]


def percentile_rank(value: Any, values: Sequence[Any]) -> Optional[float]:
    number = _valid_number(value)
    valid = sorted(_valid_numbers(values))
    if number is None or not valid:
        return None
    if len(valid) == 1:
        return 50.0
    if number <= valid[0]:
        return 0.0
    if number >= valid[-1]:
        return 100.0

    equal_positions = [index for index, item in enumerate(valid) if item == number]
    if equal_positions:
        average_position = sum(equal_positions) / len(equal_positions)
        return 100.0 * average_position / (len(valid) - 1)

    upper_index = next(index for index, item in enumerate(valid) if item > number)
    lower_index = upper_index - 1
    lower_value = valid[lower_index]
    upper_value = valid[upper_index]
    if upper_value == lower_value:
        interpolated_position = (lower_index + upper_index) / 2.0
    else:
        fraction = (number - lower_value) / (upper_value - lower_value)
        interpolated_position = lower_index + fraction
    return 100.0 * interpolated_position / (len(valid) - 1)


def _missing_policy_score(missing_policy: str, penalize_score: float = 25.0) -> Optional[float]:
    if missing_policy == "neutral":
        return 50.0
    if missing_policy == "zero":
        return 0.0
    if missing_policy == "ignore":
        return None
    if missing_policy == "penalize":
        return float(penalize_score)
    raise ValueError(f"Unknown missing_policy: {missing_policy}")


def score_with_missing_policy(
    value: Any,
    values: Sequence[Any],
    direction: str,
    missing_policy: str = "ignore",
    penalize_score: float = 25.0,
) -> Optional[float]:
    rank = percentile_rank(value, values)
    if rank is None:
        return _missing_policy_score(missing_policy, penalize_score)
    if direction in {"higher_is_better", "higher"}:
        return rank
    if direction in {"lower_is_better", "lower"}:
        return 100.0 - rank
    raise ValueError(f"Unknown direction: {direction}")


def score_higher_is_better(value: Any, values: Sequence[Any], missing_policy: str = "ignore") -> Optional[float]:
    return score_with_missing_policy(value, values, "higher_is_better", missing_policy)


def score_lower_is_better(value: Any, values: Sequence[Any], missing_policy: str = "ignore") -> Optional[float]:
    return score_with_missing_policy(value, values, "lower_is_better", missing_policy)


def safe_zscore(value: Any, values: Sequence[Any]) -> Optional[float]:
    number = _valid_number(value)
    valid = _valid_numbers(values)
    if number is None or not valid:
        return None
    mean = sum(valid) / len(valid)
    variance = sum((item - mean) ** 2 for item in valid) / len(valid)
    std_dev = math.sqrt(variance)
    if std_dev == 0:
        return 0.0
    return (number - mean) / std_dev


def _normalize_strategy_mode(strategy_mode: str) -> str:
    if strategy_mode not in VALID_STRATEGY_MODES:
        raise ValueError(f"Unknown strategy_mode: {strategy_mode}")
    return strategy_mode


def _resolve_effective_min_score(strategy_mode: str, user_min_score: Optional[float]) -> Tuple[Optional[float], str]:
    strategy_mode = _normalize_strategy_mode(strategy_mode)
    if user_min_score is not None:
        return float(user_min_score), "user"
    default_min_score = DEFAULT_MIN_SCORE_BY_MODE.get(strategy_mode)
    if default_min_score is not None:
        return float(default_min_score), "default"
    return None, "none"


def _weight_map_for(mapping: Dict[str, Dict[str, float]], strategy_mode: str) -> Dict[str, float]:
    strategy_mode = _normalize_strategy_mode(strategy_mode)
    if strategy_mode not in mapping:
        raise ValueError(f"Unknown strategy_mode: {strategy_mode}")
    return mapping[strategy_mode]


def get_strategy_weights(strategy_mode: str) -> Dict[str, float]:
    return _weight_map_for(STRATEGY_WEIGHTS, strategy_mode)


def get_fundamental_weights(strategy_mode: str) -> Dict[str, float]:
    return _weight_map_for(FUNDAMENTAL_WEIGHTS, strategy_mode)


def get_momentum_weights(strategy_mode: str) -> Dict[str, float]:
    return _weight_map_for(MOMENTUM_WEIGHTS, strategy_mode)


def get_risk_weights(strategy_mode: str) -> Dict[str, float]:
    return _weight_map_for(RISK_WEIGHTS, strategy_mode)


def _get_field_value(record: StockRecord, field_name: str) -> Any:
    candidates = [field_name]
    if field_name == "gross_margin_ttm":
        candidates = ["gross_margin_ttm", "gross_margin"]
    elif field_name == "operating_margin_ttm":
        candidates = ["operating_margin_ttm", "operating_margin"]
    elif field_name == "roe":
        candidates = ["roe", "return_on_equity"]
    elif field_name == "beta_1y":
        candidates = ["beta_1y", "beta"]
    elif field_name == "volatility_1y":
        candidates = ["volatility_1y", "volatility_63d"]
    elif field_name == "max_drawdown_1y":
        candidates = ["max_drawdown_1y", "max_drawdown_252d"]
    elif field_name == "price_vs_200dma":
        candidates = ["price_vs_200dma", "price_vs_sma200_pct"]
    elif field_name == "price_vs_sma200_pct":
        candidates = ["price_vs_sma200_pct", "price_vs_200dma"]
    elif field_name == "price_vs_sma50_pct":
        candidates = ["price_vs_sma50_pct"]
    elif field_name == "avg_dollar_volume_20d":
        candidates = ["avg_dollar_volume_20d"]
    elif field_name == "price_data_age_days":
        candidates = ["price_data_age_days", "data_age_days"]
    elif field_name == "fundamental_data_age_days":
        candidates = ["fundamental_data_age_days", "data_age_days"]
    elif field_name == "shares_data_age_days":
        candidates = ["shares_data_age_days", "data_age_days"]
    elif field_name == "data_age_days":
        candidates = ["data_age_days", "price_data_age_days"]
    elif field_name == "debt_to_equity":
        candidates = ["debt_to_equity_normalized", "debt_to_equity", "debt_to_equity_raw"]
    elif field_name == "debt_to_equity_normalized":
        candidates = ["debt_to_equity_normalized", "debt_to_equity"]
    elif field_name == "debt_to_equity_raw":
        candidates = ["debt_to_equity_raw", "debt_to_equity"]
    elif field_name == "free_cash_flow":
        candidates = ["free_cash_flow"]
    elif field_name == "ma_200":
        candidates = ["ma_200"]
    for candidate in candidates:
        if hasattr(record, candidate):
            value = getattr(record, candidate)
            if value not in ("", None):
                return value
    return None


def weighted_average_available(scores: Dict[str, Optional[float]], weights: Dict[str, float]) -> Optional[float]:
    total_weight = 0.0
    total_score = 0.0
    for key, weight in weights.items():
        value = scores.get(key)
        if value is None or weight <= 0:
            continue
        total_weight += weight
        total_score += value * weight
    if total_weight <= 0:
        return None
    return total_score / total_weight


def score_high_better(value: Optional[float], bad: float, good: float) -> Optional[float]:
    if value is None:
        return None
    if value <= bad:
        return 0.0
    if value >= good:
        return 100.0
    return 100.0 * (value - bad) / (good - bad)


def score_low_better(value: Optional[float], good: float, bad: float) -> Optional[float]:
    if value is None:
        return None
    if value <= good:
        return 100.0
    if value >= bad:
        return 0.0
    return 100.0 * (bad - value) / (bad - good)


def score_reasonable_range(value: Optional[float], low_good: float, high_good: float, high_bad: float) -> Optional[float]:
    if value is None:
        return None
    if value <= 0:
        return 0.0
    if value <= low_good:
        return 90.0
    if value <= high_good:
        return 100.0
    if value >= high_bad:
        return 0.0
    return 100.0 * (high_bad - value) / (high_bad - high_good)


def calculate_confidence_score(record: StockRecord, required_fields: Sequence[str]) -> float:
    available = 0
    for field_name in required_fields:
        value = _get_field_value(record, field_name)
        if value is not None and value != "":
            available += 1
    if not required_fields:
        return 1.0
    return available / float(len(required_fields))


def confidence_label(score: float) -> str:
    if score >= 0.85:
        return "high"
    if score >= 0.70:
        return "medium"
    if score >= 0.55:
        return "low"
    return "very_low"


def _format_price(value: Optional[float]) -> str:
    if value is None:
        return "缺資料"
    return f"${value:.2f}"


def _format_usd_millions(value: Optional[float]) -> str:
    if value is None:
        return "缺資料"
    tens_of_thousands = value / 10_000.0
    return f"{tens_of_thousands:,.0f} 萬美元"


def _format_usd_billions(value: Optional[float]) -> str:
    if value is None:
        return "缺資料"
    hundreds_of_millions = value / 100_000_000.0
    return f"{hundreds_of_millions:,.1f} 億美元"


VALID_STRATEGY_MODES = {"hybrid", "stop_checking_price"}
DEFAULT_STRATEGY_MODE = "hybrid"
MARKET_REGIME_RISK_ON = "risk_on"
MARKET_REGIME_NEUTRAL = "neutral"
MARKET_REGIME_RISK_OFF = "risk_off"
MARKET_REGIME_STATUS_ENABLED = "enabled"
MARKET_REGIME_STATUS_INSUFFICIENT = "insufficient_market_data"

STRATEGY_WEIGHTS = {
    "hybrid": {
        "fundamental": 0.40,
        "momentum": 0.35,
        "risk_safety": 0.25,
    },
    "stop_checking_price": {
        "fundamental": 0.55,
        "risk_safety": 0.30,
        "momentum": 0.15,
    },
}

FUNDAMENTAL_WEIGHTS = {
    "hybrid": {
        "growth": 0.40,
        "quality": 0.35,
        "valuation": 0.25,
    },
    "stop_checking_price": {
        "quality": 0.40,
        "growth": 0.35,
        "valuation": 0.15,
        "capital_efficiency": 0.10,
    },
}

MOMENTUM_WEIGHTS = {
    "hybrid": {
        "relative_strength": 0.45,
        "trend": 0.35,
        "persistence": 0.20,
    },
    "stop_checking_price": {
        "long_term_trend": 0.60,
        "relative_strength": 0.25,
        "persistence": 0.15,
    },
}

RISK_WEIGHTS = {
    "hybrid": {
        "volatility": 0.40,
        "beta": 0.25,
        "drawdown": 0.25,
        "liquidity_buffer": 0.10,
    },
    "stop_checking_price": {
        "drawdown": 0.30,
        "balance_sheet": 0.25,
        "volatility": 0.20,
        "earnings_stability": 0.15,
        "liquidity_buffer": 0.10,
    },
}

STOP_CHECKING_PRICE_REQUIRED_FIELDS = [
    "ticker",
    "price",
    "market_cap",
    "avg_dollar_volume_20d",
    "revenue_growth_yoy",
    "eps_growth_yoy",
    "gross_margin_ttm",
    "operating_margin_ttm",
    "roe",
    "roic",
    "free_cash_flow",
    "debt_to_equity",
    "shares_growth_yoy",
    "pe_ratio",
    "ps_ratio",
    "max_drawdown_1y",
    "volatility_1y",
    "price_vs_200dma",
    "price_data_age_days",
]

STOP_CHECKING_PRICE_SOFT_PENALTIES = [
    ("free_cash_flow", "<", 0, 8, "自由現金流為負"),
    ("operating_margin_ttm", "<", 0, 10, "營業利益率為負"),
    ("net_margin_ttm", "<", 0, 6, "淨利率為負"),
    ("pe_ratio", ">", 50, 6, "本益比偏高"),
    ("forward_pe", ">", 45, 5, "預估本益比偏高"),
    ("ps_ratio", ">", 20, 6, "股價營收比偏高"),
    ("ev_to_ebitda", ">", 35, 6, "EV/EBITDA 偏高"),
    ("peg_ratio", ">", 3, 4, "PEG 偏高"),
    ("debt_to_equity", ">", 2.0, 8, "負債權益比偏高"),
    ("net_debt_to_ebitda", ">", 4.0, 8, "淨負債 / EBITDA 偏高"),
    ("shares_growth_yoy", ">", 0.05, 6, "股本稀釋偏高"),
    ("shares_growth_3y_cagr", ">", 0.05, 8, "三年股本稀釋偏高"),
    ("revenue_growth_yoy", "<", -0.05, 6, "營收年增率明顯衰退"),
    ("eps_growth_yoy", "<", -0.10, 6, "EPS 年增率明顯衰退"),
    ("max_drawdown_1y", "<", -0.40, 8, "一年最大回撤過深"),
    ("volatility_1y", ">", 0.80, 6, "一年波動率偏高"),
    ("data_age_days", ">", 7, 8, "資料超過 7 天"),
    ("data_age_days", ">", 3, 4, "資料超過 3 天"),
]

STOP_CHECKING_PRICE_RATIO_PENALTY_FIELDS = {
    "operating_margin_ttm",
    "net_margin_ttm",
    "shares_growth_yoy",
    "shares_growth_3y_cagr",
    "revenue_growth_yoy",
    "eps_growth_yoy",
    "max_drawdown_1y",
    "volatility_1y",
}

STOP_CHECKING_PRICE_DEDUPE_COMPANY_GROUPS = {
    "GOOG": "ALPHABET",
    "GOOGL": "ALPHABET",
    "FOX": "FOX_CORP",
    "FOXA": "FOX_CORP",
    "NWS": "NEWS_CORP",
    "NWSA": "NEWS_CORP",
    "BRK-A": "BERKSHIRE_HATHAWAY",
    "BRK-B": "BERKSHIRE_HATHAWAY",
    "BRK.A": "BERKSHIRE_HATHAWAY",
    "BRK.B": "BERKSHIRE_HATHAWAY",
}

SECTOR_RELATIVE_MIN_PEERS = 30
SECTOR_METADATA_COVERAGE_GATE = 0.90
SECTOR_RELATIVE_FACTOR_FIELDS = {
    "growth": [
        ("revenue_growth_yoy", "higher"),
        ("eps_growth_yoy", "higher"),
    ],
    "quality": [
        ("gross_margin", "higher"),
        ("operating_margin", "higher"),
        ("return_on_equity", "higher"),
    ],
    "valuation": [
        ("pe_ratio", "lower"),
        ("ps_ratio", "lower"),
    ],
    "momentum": [
        ("relative_strength_252d", "higher"),
        ("price_vs_sma200_pct", "higher"),
    ],
    "risk": [
        ("volatility_63d", "lower"),
        ("beta", "lower"),
        ("max_drawdown_252d", "lower"),
        ("avg_dollar_volume_20d", "higher"),
    ],
}
SECTOR_RELATIVE_LARGE_RANK_CHANGE_THRESHOLD = 20

MAX_STOP_CHECKING_PRICE_PENALTY = 25
DEFAULT_MIN_SCORE_BY_MODE = {
    "hybrid": None,
    "stop_checking_price": None,
}
REBALANCE_MONTHS = {3, 6, 9, 12}
REBALANCE_DAY_MIN = 15


def _validate_weight_map(mode: str, mapping: Dict[str, float]) -> None:
    if abs(sum(mapping.values()) - 1.0) >= 1e-9:
        raise ValueError(f"{mode} weights must sum to 1")


for _mode, _weights in STRATEGY_WEIGHTS.items():
    _validate_weight_map(_mode, _weights)
for _mode, _weights in FUNDAMENTAL_WEIGHTS.items():
    _validate_weight_map(_mode, _weights)
for _mode, _weights in MOMENTUM_WEIGHTS.items():
    _validate_weight_map(_mode, _weights)
for _mode, _weights in RISK_WEIGHTS.items():
    _validate_weight_map(_mode, _weights)


@dataclass
class ScreenConfig:
    min_price: float = 5.0
    min_market_cap: float = 2_000_000_000.0
    min_dollar_volume_20d: float = 20_000_000.0
    max_data_age_days: int = 7
    stale_warn_days: int = 3
    fundamental_weight: float = 0.40
    momentum_weight: float = 0.35
    risk_weight: float = 0.25
    growth_weight: float = 0.40
    quality_weight: float = 0.35
    valuation_weight: float = 0.25
    relative_strength_weight: float = 0.45
    trend_weight: float = 0.35
    persistence_weight: float = 0.20
    volatility_weight: float = 0.40
    beta_weight: float = 0.25
    drawdown_weight: float = 0.25
    liquidity_buffer_weight: float = 0.10
    top_n: int = 20

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ScreenConfig":
        base = cls()
        updates = {}
        for field_name in cls.__dataclass_fields__:
            if field_name in payload and payload[field_name] is not None:
                field_type = cls.__dataclass_fields__[field_name].type
                raw_value = payload[field_name]
                if field_type in (int, Optional[int]):
                    updates[field_name] = int(raw_value)
                else:
                    updates[field_name] = float(raw_value)
        return replace(base, **updates)


@dataclass
class StockRecord:
    ticker: str
    company_name: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    price: Optional[float] = None
    market_cap: Optional[float] = None
    avg_dollar_volume_20d: Optional[float] = None
    avg_volume_20d: Optional[float] = None
    revenue_growth_yoy: Optional[float] = None
    eps_growth_yoy: Optional[float] = None
    gross_margin: Optional[float] = None
    gross_margin_ttm: Optional[float] = None
    operating_margin: Optional[float] = None
    operating_margin_ttm: Optional[float] = None
    net_margin_ttm: Optional[float] = None
    return_on_equity: Optional[float] = None
    roe: Optional[float] = None
    roic: Optional[float] = None
    free_cash_flow: Optional[float] = None
    fcf_margin: Optional[float] = None
    fcf_growth_yoy: Optional[float] = None
    revenue_growth_3y_cagr: Optional[float] = None
    pe_ratio: Optional[float] = None
    forward_pe: Optional[float] = None
    ps_ratio: Optional[float] = None
    ev_to_ebitda: Optional[float] = None
    peg_ratio: Optional[float] = None
    relative_strength_252d: Optional[float] = None
    price_vs_sma50_pct: Optional[float] = None
    price_vs_sma200_pct: Optional[float] = None
    price_vs_200dma: Optional[float] = None
    ma_200: Optional[float] = None
    beta: Optional[float] = None
    beta_1y: Optional[float] = None
    volatility_63d: Optional[float] = None
    volatility_1y: Optional[float] = None
    max_drawdown_252d: Optional[float] = None
    max_drawdown_1y: Optional[float] = None
    debt_to_equity: Optional[float] = None
    debt_to_equity_raw: Optional[float] = None
    debt_to_equity_normalized: Optional[float] = None
    net_debt_to_ebitda: Optional[float] = None
    current_ratio: Optional[float] = None
    interest_coverage: Optional[float] = None
    shares_growth_yoy: Optional[float] = None
    shares_growth_3y_cagr: Optional[float] = None
    fcf_conversion: Optional[float] = None
    operating_margin_3y_avg: Optional[float] = None
    eps_positive_years_5y: Optional[float] = None
    data_age_days: Optional[int] = None
    price_data_age_days: Optional[int] = None
    fundamental_data_age_days: Optional[int] = None
    shares_data_age_days: Optional[int] = None
    financial_statement_date: Optional[str] = None
    market_cap_timestamp: Optional[str] = None
    halted: bool = False
    is_otc: bool = False
    is_etf: bool = False
    is_adr: bool = False
    security_type: Optional[str] = None
    exchange: Optional[str] = None
    notes: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def dollar_volume_20d(self) -> Optional[float]:
        if self.avg_dollar_volume_20d is not None:
            return self.avg_dollar_volume_20d
        if self.price is not None and self.avg_volume_20d is not None:
            return self.price * self.avg_volume_20d
        return None

    @property
    def missing_required_fields(self) -> List[str]:
        missing = []
        if self.price is None:
            missing.append("price")
        if self.market_cap is None:
            missing.append("market_cap")
        if self.dollar_volume_20d is None:
            missing.append("avg_dollar_volume_20d")
        return missing


@dataclass
class ScreenResult:
    ticker: str
    strategy_mode: str
    total_score: Optional[float]
    raw_score: Optional[float]
    adjusted_score: Optional[float]
    fundamental_score: Optional[float]
    momentum_score: Optional[float]
    risk_safety_score: Optional[float]
    factor_scores: Dict[str, Optional[float]]
    reasons: List[str]
    risk_warnings: List[str]
    confidence_notes: List[str]
    penalties: List[Dict[str, Any]] = field(default_factory=list)
    confidence_score: Optional[float] = None
    confidence_label: Optional[str] = None
    company_snapshot: Optional[Dict[str, Any]] = None
    suggested_action: Optional[str] = None
    hard_exclusion: Optional[bool] = None
    excluded_reason: Optional[str] = None
    exclusion_reasons: List[str] = field(default_factory=list)
    exclusion_details: List[Dict[str, Any]] = field(default_factory=list)
    record: Optional[StockRecord] = None
    penalty_score: Optional[float] = None
    confidence_multiplier: Optional[float] = None
    final_score: Optional[float] = None
    data_quality_score: Optional[float] = None
    data_quality_flags: List[str] = field(default_factory=list)
    normalization_notes: List[str] = field(default_factory=list)
    action_cap_reason: Optional[str] = None
    sector_relative_score_preview: Optional[float] = None
    sector_relative_rank_preview: Optional[int] = None
    sector_relative_score_delta: Optional[float] = None
    sector_relative_rank_delta: Optional[int] = None
    sector_relative_notes: List[str] = field(default_factory=list)
    sector_relative_factor_scores: Dict[str, Optional[float]] = field(default_factory=dict)
    sector_relative_peer_source: Optional[str] = None
    sector_relative_peer_count: Optional[int] = None
    sector_relative_peer_reason: Optional[str] = None
    official_score_source: Optional[str] = None
    base_total_score: Optional[float] = None
    market_regime_score_delta: Optional[float] = None
    legacy_total_score: Optional[float] = None
    legacy_raw_score: Optional[float] = None
    legacy_adjusted_score: Optional[float] = None
    legacy_fundamental_score: Optional[float] = None
    legacy_momentum_score: Optional[float] = None
    legacy_risk_safety_score: Optional[float] = None

    @property
    def is_candidate(self) -> bool:
        return self.excluded_reason is None


@dataclass
class ScreeningReport:
    as_of: Optional[date]
    config: ScreenConfig
    strategy_mode: str
    review_mode: str
    hard_pass_count: int
    candidates: List[ScreenResult]
    excluded: List[ScreenResult]
    universe_size: int
    hard_excluded: List[ScreenResult] = field(default_factory=list)
    soft_penalties: List[Dict[str, Any]] = field(default_factory=list)
    missing_data_warnings: List[Dict[str, Any]] = field(default_factory=list)
    min_score: Optional[float] = None
    top_n: int = 20
    dedupe_company: bool = False
    deduped: List[ScreenResult] = field(default_factory=list)
    retry_failed_count: int = 0
    fetch_failed_count: int = 0
    dedupe_removed_count: int = 0
    effective_min_score_source: str = "none"
    ranking_style: str = "balanced"
    top_n_average_total_score: Optional[float] = None
    top_n_average_fundamental_score: Optional[float] = None
    top_n_average_momentum_score: Optional[float] = None
    top_n_average_risk_safety_score: Optional[float] = None
    high_risk_candidate_count: int = 0
    expensive_candidate_count: int = 0
    high_volatility_candidate_count: int = 0
    deep_drawdown_candidate_count: int = 0
    missing_data_candidate_count: int = 0
    sector_aware_official_scoring: bool = True
    sector_aware_status: str = "enabled"
    sector_metadata_coverage: float = 0.0
    industry_metadata_coverage: float = 0.0
    metadata_fetch_failed_count: int = 0
    metadata_missing_count: int = 0
    sector_aware_shadow_mode: bool = True
    sector_aware_preview_available_count: int = 0
    sector_aware_preview_missing_count: int = 0
    sector_aware_average_score_delta: Optional[float] = None
    sector_aware_rank_changed_count: int = 0
    sector_aware_top_movers_up: List[Dict[str, Any]] = field(default_factory=list)
    sector_aware_top_movers_down: List[Dict[str, Any]] = field(default_factory=list)
    sector_aware_preview_coverage: Optional[float] = None
    sector_aware_score_correlation_with_current: Optional[float] = None
    sector_aware_top_10_overlap: Optional[int] = None
    sector_aware_top_10_overlap_total: int = 0
    sector_aware_large_rank_change_count: int = 0
    sector_aware_large_rank_change_threshold: int = SECTOR_RELATIVE_LARGE_RANK_CHANGE_THRESHOLD
    sector_aware_largest_movers: List[Dict[str, Any]] = field(default_factory=list)
    sector_aware_sector_peer_used_count: int = 0
    sector_aware_industry_peer_used_count: int = 0
    sector_aware_sector_only_peer_used_count: int = 0
    sector_aware_universe_fallback_count: int = 0
    sector_aware_missing_sector_count: int = 0
    sector_aware_universe_missing_metadata_count: int = 0
    sector_aware_not_scored_disabled_count: int = 0
    sector_aware_average_peer_count: Optional[float] = None
    sector_aware_min_peer_count: Optional[int] = None
    sector_aware_max_peer_count: Optional[int] = None
    market_context: Dict[str, Any] = field(default_factory=dict)
    configured_composite_weights: Dict[str, float] = field(default_factory=dict)
    effective_composite_weights: Dict[str, float] = field(default_factory=dict)
    market_regime: str = MARKET_REGIME_NEUTRAL
    market_regime_status: str = MARKET_REGIME_STATUS_INSUFFICIENT
    market_regime_signals: Dict[str, str] = field(default_factory=dict)


def _canonicalize_record(payload: Dict[str, Any]) -> StockRecord:
    ticker = _normalize_ticker(_pick(payload, "ticker", "symbol", "code"))
    if not ticker:
        raise ValueError("missing ticker")
    return StockRecord(
        ticker=ticker,
        company_name=_pick(payload, "company_name", "name"),
        sector=_clean_metadata_text(_pick(payload, "sector")),
        industry=_clean_metadata_text(_pick(payload, "industry")),
        price=_coerce_float(_pick(payload, "price", "last_price", "close")),
        market_cap=_coerce_float(_pick(payload, "market_cap", "marketcap")),
        avg_dollar_volume_20d=_coerce_float(_pick(payload, "avg_dollar_volume_20d", "dollar_volume_20d")),
        avg_volume_20d=_coerce_float(_pick(payload, "avg_volume_20d", "avg_daily_volume", "volume_20d")),
        revenue_growth_yoy=_coerce_float(_pick(payload, "revenue_growth_yoy", "revenue_growth")),
        eps_growth_yoy=_coerce_float(_pick(payload, "eps_growth_yoy", "eps_growth")),
        gross_margin=_coerce_float(_pick(payload, "gross_margin", "gross_margin_pct")),
        gross_margin_ttm=_coerce_float(_pick(payload, "gross_margin_ttm", "gross_margin_pct_ttm", "gross_margin")),
        operating_margin=_coerce_float(_pick(payload, "operating_margin", "operating_margin_pct")),
        operating_margin_ttm=_coerce_float(_pick(payload, "operating_margin_ttm", "operating_margin_pct_ttm", "operating_margin")),
        net_margin_ttm=_coerce_float(_pick(payload, "net_margin_ttm", "net_margin")),
        return_on_equity=_coerce_float(_pick(payload, "return_on_equity", "roe")),
        roe=_coerce_float(_pick(payload, "roe", "return_on_equity")),
        roic=_coerce_float(_pick(payload, "roic", "return_on_invested_capital")),
        free_cash_flow=_coerce_float(_pick(payload, "free_cash_flow", "fcf")),
        fcf_margin=_coerce_float(_pick(payload, "fcf_margin")),
        fcf_growth_yoy=_coerce_float(_pick(payload, "fcf_growth_yoy", "free_cash_flow_growth_yoy")),
        revenue_growth_3y_cagr=_coerce_float(_pick(payload, "revenue_growth_3y_cagr", "rev_growth_3y_cagr")),
        pe_ratio=_coerce_float(_pick(payload, "pe_ratio", "trailing_pe")),
        forward_pe=_coerce_float(_pick(payload, "forward_pe")),
        ps_ratio=_coerce_float(_pick(payload, "ps_ratio", "price_to_sales")),
        ev_to_ebitda=_coerce_float(_pick(payload, "ev_to_ebitda", "ev_ebitda")),
        peg_ratio=_coerce_float(_pick(payload, "peg_ratio")),
        relative_strength_252d=_coerce_float(_pick(payload, "relative_strength_252d", "rs_252d_percentile", "relative_strength_percentile")),
        price_vs_sma50_pct=_coerce_float(_pick(payload, "price_vs_sma50_pct", "above_sma50_pct")),
        price_vs_sma200_pct=_coerce_float(_pick(payload, "price_vs_sma200_pct", "above_sma200_pct")),
        price_vs_200dma=_coerce_float(_pick(payload, "price_vs_200dma", "above_200dma_pct")),
        ma_200=_coerce_float(_pick(payload, "ma_200", "sma200", "200dma")),
        beta=_coerce_float(_pick(payload, "beta")),
        beta_1y=_coerce_float(_pick(payload, "beta_1y", "beta")),
        volatility_63d=_coerce_float(_pick(payload, "volatility_63d", "realized_volatility_63d")),
        volatility_1y=_coerce_float(_pick(payload, "volatility_1y", "annualized_volatility_1y", "volatility")),
        max_drawdown_252d=_coerce_float(_pick(payload, "max_drawdown_252d", "drawdown_252d")),
        max_drawdown_1y=_coerce_float(_pick(payload, "max_drawdown_1y", "drawdown_1y", "max_drawdown")),
        debt_to_equity_raw=_coerce_float(_pick(payload, "debt_to_equity_raw", "debt_to_equity", "de_ratio")),
        debt_to_equity_normalized=_coerce_float(_pick(payload, "debt_to_equity_normalized")),
        debt_to_equity=_normalize_debt_to_equity(
            _coerce_float(_pick(payload, "debt_to_equity_normalized", "debt_to_equity_raw", "debt_to_equity", "de_ratio"))
        ),
        net_debt_to_ebitda=_coerce_float(_pick(payload, "net_debt_to_ebitda")),
        current_ratio=_coerce_float(_pick(payload, "current_ratio")),
        interest_coverage=_coerce_float(_pick(payload, "interest_coverage")),
        shares_growth_yoy=_coerce_float(_pick(payload, "shares_growth_yoy", "share_count_growth_yoy")),
        shares_growth_3y_cagr=_coerce_float(_pick(payload, "shares_growth_3y_cagr")),
        fcf_conversion=_coerce_float(_pick(payload, "fcf_conversion")),
        operating_margin_3y_avg=_coerce_float(_pick(payload, "operating_margin_3y_avg")),
        eps_positive_years_5y=_coerce_float(_pick(payload, "eps_positive_years_5y")),
        data_age_days=_coerce_int(_pick(payload, "data_age_days")),
        price_data_age_days=_coerce_int(_pick(payload, "price_data_age_days", "quote_data_age_days", "market_data_age_days", "data_age_days")),
        fundamental_data_age_days=_coerce_int(_pick(payload, "fundamental_data_age_days", "financial_data_age_days", "data_age_days")),
        shares_data_age_days=_coerce_int(_pick(payload, "shares_data_age_days", "share_data_age_days", "data_age_days")),
        financial_statement_date=_pick(payload, "financial_statement_date", "latest_financial_statement_date"),
        market_cap_timestamp=_pick(payload, "market_cap_timestamp", "quote_timestamp", "fetched_at"),
        halted=_coerce_bool(_pick(payload, "halted")),
        is_otc=_coerce_bool(_pick(payload, "is_otc")),
        is_etf=_coerce_bool(_pick(payload, "is_etf")),
        is_adr=_coerce_bool(_pick(payload, "is_adr")),
        security_type=_pick(payload, "security_type"),
        exchange=_pick(payload, "exchange"),
        notes=_pick(payload, "notes"),
        raw=dict(payload),
    )


def load_records(path: Path) -> List[StockRecord]:
    if not path.exists():
        raise FileNotFoundError(path)
    if path.suffix.lower() == ".json":
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {path.name}: {exc.msg}") from exc
        if isinstance(payload, dict) and "records" in payload:
            payload = payload["records"]
        if not isinstance(payload, list):
            raise ValueError("JSON input must be a list or an object with a 'records' key")
        return [_canonicalize_record(item) for item in payload]
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        records = []
        for line_number, line in enumerate(path.read_text().splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(_canonicalize_record(json.loads(line)))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL in {path.name} line {line_number}: {exc.msg}") from exc
        return records
    if path.suffix.lower() == ".csv":
        records = []
        with path.open(newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                records.append(_canonicalize_record(row))
        return records
    raise ValueError("Unsupported input format. Use CSV, JSON, or JSONL.")


def _fundamental_score(record: StockRecord) -> Tuple[Optional[float], Dict[str, Optional[float]]]:
    growth_components = [
        (_linear_score(record.revenue_growth_yoy, -10.0, 35.0, higher_is_better=True), 0.60),
        (_linear_score(record.eps_growth_yoy, -10.0, 40.0, higher_is_better=True), 0.40),
    ]
    quality_components = [
        (_linear_score(record.gross_margin, 10.0, 60.0, higher_is_better=True), 0.35),
        (_linear_score(record.operating_margin, 5.0, 40.0, higher_is_better=True), 0.35),
        (_linear_score(record.return_on_equity, 0.0, 30.0, higher_is_better=True), 0.30),
    ]
    valuation_components = [
        (_linear_score(record.pe_ratio, 8.0, 45.0, higher_is_better=False), 0.60),
        (_linear_score(record.ps_ratio, 1.0, 10.0, higher_is_better=False), 0.40),
    ]

    growth = _average([item for item in growth_components if item[0] is not None])
    quality = _average([item for item in quality_components if item[0] is not None])
    valuation = _average([item for item in valuation_components if item[0] is not None])

    fundamental = _average(
        _compact(
            [
                (growth, 0.40) if growth is not None else None,
                (quality, 0.35) if quality is not None else None,
                (valuation, 0.25) if valuation is not None else None,
            ]
        )
    )
    return fundamental, {
        "growth": _safe_round(growth),
        "quality": _safe_round(quality),
        "valuation": _safe_round(valuation),
    }


def _momentum_score(record: StockRecord) -> Tuple[Optional[float], Dict[str, Optional[float]]]:
    relative_strength = None
    if record.relative_strength_252d is not None:
        relative_strength = max(0.0, min(100.0, record.relative_strength_252d))

    trend_components = [
        (_symmetric_score(record.price_vs_sma50_pct, -20.0, 20.0), 0.55),
        (_symmetric_score(record.price_vs_sma200_pct, -25.0, 25.0), 0.45),
    ]
    persistence_components = [
        (_linear_score(record.price_vs_sma50_pct, -10.0, 20.0, higher_is_better=True), 0.50),
        (_linear_score(record.price_vs_sma200_pct, -15.0, 25.0, higher_is_better=True), 0.50),
    ]

    trend = _average([item for item in trend_components if item[0] is not None])
    persistence = _average([item for item in persistence_components if item[0] is not None])

    momentum = _average(
        _compact(
            [
                (relative_strength, 0.45) if relative_strength is not None else None,
                (trend, 0.35) if trend is not None else None,
                (persistence, 0.20) if persistence is not None else None,
            ]
        )
    )
    return momentum, {
        "relative_strength": _safe_round(relative_strength),
        "trend": _safe_round(trend),
        "persistence": _safe_round(persistence),
    }


def _risk_score(record: StockRecord) -> Tuple[Optional[float], Dict[str, Optional[float]]]:
    volatility = _linear_score(record.volatility_63d, 12.0, 55.0, higher_is_better=False)
    beta = _linear_score(record.beta, 0.8, 2.0, higher_is_better=False)
    drawdown = _linear_score(record.max_drawdown_252d, 8.0, 45.0, higher_is_better=False)
    liquidity = _linear_score(record.dollar_volume_20d, 20_000_000.0, 250_000_000.0, higher_is_better=True)
    risk_safety = _average(
        _compact(
            [
                (volatility, 0.40) if volatility is not None else None,
                (beta, 0.25) if beta is not None else None,
                (drawdown, 0.25) if drawdown is not None else None,
                (liquidity, 0.10) if liquidity is not None else None,
            ]
        )
    )
    return risk_safety, {
        "volatility": _safe_round(volatility),
        "beta": _safe_round(beta),
        "drawdown": _safe_round(drawdown),
        "liquidity": _safe_round(liquidity),
    }


def _confidence_notes(record: StockRecord, factor_scores: Dict[str, Optional[float]]) -> List[str]:
    notes = []
    price_data_age_days = _record_age_days(record, "price")
    if price_data_age_days is not None and price_data_age_days > 3:
        notes.append(f"價格資料已 {price_data_age_days} 天未更新，判斷會比較舊")
    if record.revenue_growth_yoy is None or record.eps_growth_yoy is None:
        missing = [field for field in ("revenue_growth_yoy", "eps_growth_yoy") if getattr(record, field) is None]
        notes.append(f"缺少 {', '.join(missing)}；成長因子會只靠剩下的欄位重算，結果比較不穩")
    if record.gross_margin is None or record.operating_margin is None or record.return_on_equity is None:
        missing = [field for field in ("gross_margin", "operating_margin", "return_on_equity") if getattr(record, field) is None]
        notes.append(f"缺少 {', '.join(missing)}；品質因子會比較粗略")
    if record.pe_ratio is None and record.ps_ratio is None:
        notes.append("缺少 pe_ratio, ps_ratio；估值判斷只能靠其他因子")
    if record.relative_strength_252d is None or record.price_vs_sma50_pct is None or record.price_vs_sma200_pct is None:
        missing = [field for field in ("relative_strength_252d", "price_vs_sma50_pct", "price_vs_sma200_pct") if getattr(record, field) is None]
        notes.append(f"缺少 {', '.join(missing)}；動量判斷會比較不穩")
    if record.volatility_63d is None or record.max_drawdown_252d is None or record.beta is None:
        missing = [field for field in ("volatility_63d", "max_drawdown_252d", "beta") if getattr(record, field) is None]
        notes.append(f"缺少 {', '.join(missing)}；風險安全分數會比較保守")
    if len(record.missing_required_fields) > 0:
        notes.append(f"缺少必要欄位: {', '.join(record.missing_required_fields)}")
    if record.notes:
        notes.append(record.notes)
    return notes


def _risk_warnings(record: StockRecord) -> List[str]:
    warnings = []
    if record.beta is not None and record.beta >= 1.6:
        warnings.append("β 偏高")
    if record.volatility_63d is not None and record.volatility_63d >= 35.0:
        warnings.append("近三個月波動偏高")
    if record.max_drawdown_252d is not None and record.max_drawdown_252d >= 25.0:
        warnings.append("近期回撤偏深")
    if record.debt_to_equity is not None and record.debt_to_equity >= 2.0:
        warnings.append("槓桿偏高")
    if record.pe_ratio is not None and record.pe_ratio >= 45.0:
        warnings.append("估值偏貴")
    price_data_age_days = _record_age_days(record, "price")
    if price_data_age_days is not None and price_data_age_days > 7:
        warnings.append("價格資料過舊")
    return warnings


def _hybrid_missing_factor_fields(record: StockRecord) -> List[str]:
    fields = (
        "revenue_growth_yoy",
        "eps_growth_yoy",
        "gross_margin",
        "operating_margin",
        "return_on_equity",
        "pe_ratio",
        "ps_ratio",
        "relative_strength_252d",
        "price_vs_sma50_pct",
        "price_vs_sma200_pct",
        "volatility_63d",
        "max_drawdown_252d",
        "beta",
    )
    return [field_name for field_name in fields if getattr(record, field_name) is None]


def assign_hybrid_action(record: StockRecord, total_score: Optional[float], risk_safety_score: Optional[float]) -> Tuple[str, Optional[str]]:
    missing_fields = _hybrid_missing_factor_fields(record)
    if missing_fields:
        return (
            "CANDIDATE_DATA_LIMITED",
            f"缺少 {', '.join(missing_fields)}，僅適合作為資料待補候選。",
        )
    if risk_safety_score is not None and risk_safety_score < 40:
        return (
            "CANDIDATE_HIGH_RISK",
            "風險安全分數過低，僅適合作為高風險候選觀察。",
        )
    return "CANDIDATE", None


def _maybe_ratio(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    if abs(value) > 1.5 and abs(value) <= 500:
        return value / 100.0
    return value


def _normalize_debt_to_equity(raw_value: Optional[float]) -> Optional[float]:
    if raw_value is None:
        return None
    if raw_value > 10:
        return raw_value / 100.0
    return raw_value


def _debt_to_equity_normalized(record: StockRecord) -> Optional[float]:
    if record.debt_to_equity_normalized is not None:
        return record.debt_to_equity_normalized
    if record.debt_to_equity is not None:
        return _normalize_debt_to_equity(record.debt_to_equity)
    if record.debt_to_equity_raw is not None:
        return _normalize_debt_to_equity(record.debt_to_equity_raw)
    return None


def _is_sector_aware_debt_sector(record: StockRecord) -> bool:
    sector_text = " ".join(
        part for part in [record.sector, record.industry, record.security_type] if part
    ).lower()
    return any(
        keyword in sector_text
        for keyword in ("financial", "bank", "insurance", "reit", "real estate", "utility")
    )


def is_rebalance_window(as_of: Optional[date]) -> bool:
    if as_of is None:
        return False
    return as_of.month in REBALANCE_MONTHS and as_of.day >= REBALANCE_DAY_MIN


def _stop_field(record: StockRecord, *names: str) -> Optional[float]:
    for name in names:
        value = _get_field_value(record, name)
        if value is not None:
            return _coerce_float(value)
    return None


def _stop_ratio_field(record: StockRecord, *names: str) -> Optional[float]:
    return _maybe_ratio(_stop_field(record, *names))


def apply_stop_checking_price_extra_filters(record: StockRecord) -> List[str]:
    return [item["reason"] for item in apply_stop_checking_price_extra_filter_details(record)]


def _borderline_exclusion_severity(
    value: Optional[float],
    threshold: float,
    *,
    direction: str,
    tolerance: float = 0.05,
) -> str:
    if value is None:
        return "normal"
    threshold_band = abs(threshold) * tolerance
    if threshold_band == 0:
        threshold_band = tolerance
    if direction == "above" and value > threshold and value <= threshold + threshold_band:
        return "borderline"
    if direction == "below" and value < threshold and value >= threshold - threshold_band:
        return "borderline"
    return "normal"


def _with_borderline_reason(reason: str, severity: str) -> str:
    if severity == "borderline":
        return f"{reason}（邊界剔除，建議人工複查資料）"
    return reason


def apply_stop_checking_price_extra_filter_details(record: StockRecord) -> List[Dict[str, Any]]:
    details: List[Dict[str, Any]] = []
    data_age_days = _record_age_days(record, "price")
    operating_margin_ttm = _stop_ratio_field(record, "operating_margin_ttm")
    operating_margin_raw = _stop_field(record, "operating_margin_ttm")
    debt_to_equity = _debt_to_equity_normalized(record)
    debt_to_equity_raw = record.debt_to_equity_raw if record.debt_to_equity_raw is not None else record.debt_to_equity
    shares_growth_yoy = _stop_ratio_field(record, "shares_growth_yoy")
    shares_growth_raw = _stop_field(record, "shares_growth_yoy")
    sector_aware = _is_sector_aware_debt_sector(record)
    if data_age_days is not None and data_age_days > 30:
        severity = _borderline_exclusion_severity(float(data_age_days), 30.0, direction="above")
        reason = _with_borderline_reason("價格資料超過 30 天，過舊", severity)
        details.append(
            {
                "reason": reason,
                "category": "stale_data",
                "severity": severity,
                "field": "price_data_age_days",
                "raw_value": data_age_days,
                "normalized_value": data_age_days,
                "threshold": 30,
            }
        )
    if operating_margin_ttm is not None and operating_margin_ttm < -0.20:
        severity = _borderline_exclusion_severity(operating_margin_ttm, -0.20, direction="below")
        reason = _with_borderline_reason("營業利益率嚴重為負", severity)
        details.append(
            {
                "reason": reason,
                "category": "profitability",
                "severity": severity,
                "field": "operating_margin_ttm",
                "raw_value": operating_margin_raw,
                "normalized_value": operating_margin_ttm,
                "threshold": -0.20,
            }
        )
    if debt_to_equity is not None and not sector_aware and debt_to_equity > 10:
        severity = _borderline_exclusion_severity(debt_to_equity, 10.0, direction="above")
        reason = _with_borderline_reason("負債權益比極高", severity)
        details.append(
            {
                "reason": reason,
                "category": "leverage",
                "severity": severity,
                "field": "debt_to_equity",
                "raw_value": debt_to_equity_raw,
                "normalized_value": debt_to_equity,
                "threshold": 10,
            }
        )
    if shares_growth_yoy is not None and shares_growth_yoy > 0.20:
        severity = _borderline_exclusion_severity(shares_growth_yoy, 0.20, direction="above")
        reason = _with_borderline_reason("股本稀釋嚴重", severity)
        details.append(
            {
                "reason": reason,
                "category": "dilution",
                "severity": severity,
                "field": "shares_growth_yoy",
                "raw_value": shares_growth_raw,
                "normalized_value": shares_growth_yoy,
                "threshold": 0.20,
            }
        )
    return details


def calculate_stop_checking_price_penalties(record: StockRecord) -> Tuple[List[Dict[str, Any]], float]:
    penalties: List[Dict[str, Any]] = []
    total_points = 0.0
    stale_points = 0.0
    data_age_days = _record_age_days(record, "price")
    debt_to_equity = _debt_to_equity_normalized(record)
    sector_aware = _is_sector_aware_debt_sector(record)
    if data_age_days is not None:
        if data_age_days > 7:
            stale_points = 8.0
            penalties.append(
                {
                    "reason": "資料超過 7 天",
                    "points": 8.0,
                    "field": "price_data_age_days",
                    "value": data_age_days,
                }
            )
        elif data_age_days > 3:
            stale_points = 4.0
            penalties.append(
                {
                    "reason": "資料超過 3 天",
                    "points": 4.0,
                    "field": "price_data_age_days",
                    "value": data_age_days,
                }
            )
    total_points += stale_points
    for field_name, operator, threshold, points, reason in STOP_CHECKING_PRICE_SOFT_PENALTIES:
        if field_name == "data_age_days":
            continue
        if field_name == "debt_to_equity":
            value = debt_to_equity
        elif field_name in STOP_CHECKING_PRICE_RATIO_PENALTY_FIELDS:
            value = _stop_ratio_field(record, field_name)
        else:
            value = _stop_field(record, field_name)
        if value is None:
            continue
        matched = False
        if operator == "<" and value < float(threshold):
            matched = True
        elif operator == ">" and value > float(threshold):
            matched = True
        if field_name == "debt_to_equity" and sector_aware and value <= 4.0:
            matched = False
        if matched:
            penalties.append(
                {
                    "reason": reason,
                    "points": float(points),
                    "field": field_name,
                    "value": value,
                }
            )
            total_points += float(points)
    total_points = min(MAX_STOP_CHECKING_PRICE_PENALTY, total_points)
    if total_points < stale_points:
        total_points = stale_points
    return penalties, total_points


def _stop_quality_score(record: StockRecord) -> Optional[float]:
    gross_margin = _stop_ratio_field(record, "gross_margin_ttm")
    operating_margin = _stop_ratio_field(record, "operating_margin_ttm")
    free_cash_flow = _stop_field(record, "free_cash_flow")
    roe = _stop_ratio_field(record, "roe")
    debt_to_equity = _debt_to_equity_normalized(record)
    sector_aware = _is_sector_aware_debt_sector(record)
    debt_good, debt_bad = (1.0, 5.0) if sector_aware else (0.50, 2.50)
    scores = {
        "gross_margin_score": score_high_better(gross_margin, 0.20, 0.60),
        "operating_margin_score": score_high_better(operating_margin, 0.00, 0.25),
        "free_cash_flow_score": score_high_better(free_cash_flow, 0.0, 1_000_000_000.0),
        "roe_score": score_high_better(roe, 0.00, 0.20),
        "debt_control_score": score_low_better(debt_to_equity, debt_good, debt_bad),
    }
    return weighted_average_available(scores, {
        "gross_margin_score": 0.15,
        "operating_margin_score": 0.25,
        "free_cash_flow_score": 0.25,
        "roe_score": 0.20,
        "debt_control_score": 0.15,
    })


def _stop_growth_score(record: StockRecord) -> Optional[float]:
    scores = {
        "revenue_growth_score": score_high_better(_stop_ratio_field(record, "revenue_growth_yoy"), -0.05, 0.20),
        "eps_growth_score": score_high_better(_stop_ratio_field(record, "eps_growth_yoy"), -0.10, 0.20),
        "fcf_growth_score": score_high_better(_stop_ratio_field(record, "fcf_growth_yoy"), -0.10, 0.20),
        "revenue_growth_3y_cagr_score": score_high_better(_stop_ratio_field(record, "revenue_growth_3y_cagr"), 0.00, 0.20),
    }
    return weighted_average_available(scores, {
        "revenue_growth_score": 0.35,
        "eps_growth_score": 0.30,
        "fcf_growth_score": 0.20,
        "revenue_growth_3y_cagr_score": 0.15,
    })


def _stop_valuation_score(record: StockRecord) -> Optional[float]:
    pe_ratio = _stop_field(record, "pe_ratio")
    forward_pe = _stop_field(record, "forward_pe")
    ps_ratio = _stop_field(record, "ps_ratio")
    ev_to_ebitda = _stop_field(record, "ev_to_ebitda")
    peg_ratio = _stop_field(record, "peg_ratio")
    pe_score = None if pe_ratio is None or pe_ratio <= 0 else score_reasonable_range(pe_ratio, 10.0, 30.0, 70.0)
    forward_pe_score = None if forward_pe is None or forward_pe <= 0 else score_reasonable_range(forward_pe, 10.0, 28.0, 60.0)
    ps_score = None if ps_ratio is None or ps_ratio <= 0 else score_reasonable_range(ps_ratio, 1.0, 8.0, 25.0)
    ev_ebitda_score = None if ev_to_ebitda is None or ev_to_ebitda <= 0 else score_reasonable_range(ev_to_ebitda, 6.0, 18.0, 40.0)
    peg_score = None if peg_ratio is None or peg_ratio <= 0 else score_reasonable_range(peg_ratio, 0.8, 1.8, 5.0)
    return weighted_average_available(
        {
            "pe_score": pe_score,
            "forward_pe_score": forward_pe_score,
            "ps_score": ps_score,
            "ev_ebitda_score": ev_ebitda_score,
            "peg_score": peg_score,
        },
        {
            "pe_score": 0.30,
            "forward_pe_score": 0.20,
            "ps_score": 0.20,
            "ev_ebitda_score": 0.20,
            "peg_score": 0.10,
        },
    )


def _stop_capital_efficiency_score(record: StockRecord) -> Optional[float]:
    roic = _stop_ratio_field(record, "roic")
    roe = _stop_ratio_field(record, "roe")
    fcf_conversion = _stop_ratio_field(record, "fcf_conversion")
    share_dilution = _stop_ratio_field(record, "shares_growth_yoy")
    scores = {
        "roic_score": score_high_better(roic, 0.00, 0.15),
        "roe_score": score_high_better(roe, 0.00, 0.20),
        "fcf_conversion_score": score_high_better(fcf_conversion, 0.30, 0.90),
        "share_dilution_score": score_low_better(share_dilution, 0.00, 0.10),
    }
    return weighted_average_available(scores, {
        "roic_score": 0.35,
        "roe_score": 0.25,
        "fcf_conversion_score": 0.25,
        "share_dilution_score": 0.15,
    })


def _stop_fundamental_score(record: StockRecord) -> Tuple[Optional[float], Dict[str, Optional[float]]]:
    quality = _stop_quality_score(record)
    growth = _stop_growth_score(record)
    valuation = _stop_valuation_score(record)
    capital_efficiency = _stop_capital_efficiency_score(record)
    fundamental = weighted_average_available(
        {
            "quality": quality,
            "growth": growth,
            "valuation": valuation,
            "capital_efficiency": capital_efficiency,
        },
        FUNDAMENTAL_WEIGHTS["stop_checking_price"],
    )
    return fundamental, {
        "quality": _safe_round(quality),
        "growth": _safe_round(growth),
        "valuation": _safe_round(valuation),
        "capital_efficiency": _safe_round(capital_efficiency),
    }


def _stop_long_term_trend_score(record: StockRecord) -> Optional[float]:
    value = _stop_ratio_field(record, "price_vs_200dma")
    if value is None and record.price is not None and _get_field_value(record, "ma_200") not in (None, ""):
        ma_200 = _coerce_float(_get_field_value(record, "ma_200"))
        if ma_200 and ma_200 > 0:
            value = (record.price / ma_200) - 1.0
    if value is None:
        return None
    if value >= 0.10:
        return 100.0
    if value >= 0.00:
        return 80.0
    if value >= -0.10:
        return 50.0
    if value >= -0.20:
        return 25.0
    return 0.0


def _stop_momentum_score(record: StockRecord) -> Tuple[Optional[float], Dict[str, Optional[float]]]:
    long_term_trend = _stop_long_term_trend_score(record)
    relative_strength = None
    if record.relative_strength_252d is not None:
        relative_strength = max(0.0, min(100.0, record.relative_strength_252d))
    persistence = _average(
        _compact(
            [
                (score_high_better(_stop_ratio_field(record, "price_vs_sma50_pct"), -0.10, 0.20), 0.50)
                if _stop_ratio_field(record, "price_vs_sma50_pct") is not None
                else None,
                (score_high_better(_stop_ratio_field(record, "price_vs_sma200_pct"), -0.15, 0.25), 0.50)
                if _stop_ratio_field(record, "price_vs_sma200_pct") is not None
                else None,
            ]
        )
    )
    momentum = weighted_average_available(
        {
            "long_term_trend": long_term_trend,
            "relative_strength": relative_strength,
            "persistence": persistence,
        },
        MOMENTUM_WEIGHTS["stop_checking_price"],
    )
    return momentum, {
        "long_term_trend": _safe_round(long_term_trend),
        "relative_strength": _safe_round(relative_strength),
        "persistence": _safe_round(persistence),
    }


def _stop_balance_sheet_score(record: StockRecord) -> Optional[float]:
    debt_to_equity = _debt_to_equity_normalized(record)
    net_debt_to_ebitda = _stop_field(record, "net_debt_to_ebitda")
    current_ratio = _stop_field(record, "current_ratio")
    interest_coverage = _stop_field(record, "interest_coverage")
    sector_aware = _is_sector_aware_debt_sector(record)
    debt_good, debt_bad = (1.0, 5.0) if sector_aware else (0.50, 2.50)
    scores = {
        "debt_to_equity_score": score_low_better(debt_to_equity, debt_good, debt_bad),
        "net_debt_to_ebitda_score": score_low_better(net_debt_to_ebitda, 1.0, 4.0),
        "current_ratio_score": score_high_better(current_ratio, 1.0, 2.0),
        "interest_coverage_score": score_high_better(interest_coverage, 2.0, 8.0),
    }
    return weighted_average_available(scores, {
        "debt_to_equity_score": 0.35,
        "net_debt_to_ebitda_score": 0.30,
        "current_ratio_score": 0.15,
        "interest_coverage_score": 0.20,
    })


def _stop_earnings_stability_score(record: StockRecord) -> Optional[float]:
    eps_positive_years_5y = _stop_field(record, "eps_positive_years_5y")
    operating_margin_ttm = _stop_ratio_field(record, "operating_margin_ttm")
    revenue_growth_yoy = _stop_ratio_field(record, "revenue_growth_yoy")
    eps_growth_yoy = _stop_ratio_field(record, "eps_growth_yoy")
    scores = {
        "positive_eps_score": score_high_better(eps_positive_years_5y, 0.0, 5.0),
        "positive_operating_margin_score": score_high_better(operating_margin_ttm, 0.00, 0.15),
        "revenue_not_declining_score": score_high_better(revenue_growth_yoy, -0.10, 0.05),
        "eps_not_declining_score": score_high_better(eps_growth_yoy, -0.15, 0.05),
    }
    return weighted_average_available(scores, {
        "positive_eps_score": 0.30,
        "positive_operating_margin_score": 0.30,
        "revenue_not_declining_score": 0.20,
        "eps_not_declining_score": 0.20,
    })


def _stop_drawdown_score(record: StockRecord) -> Optional[float]:
    drawdown = _stop_ratio_field(record, "max_drawdown_1y")
    if drawdown is None:
        drawdown = _stop_ratio_field(record, "max_drawdown_252d")
    if drawdown is None:
        return None
    return score_high_better(drawdown, -0.50, -0.15)


def _stop_volatility_score(record: StockRecord) -> Optional[float]:
    volatility = _stop_ratio_field(record, "volatility_1y")
    if volatility is None:
        volatility = _stop_ratio_field(record, "volatility_63d")
    if volatility is None:
        return None
    return score_low_better(volatility, 0.20, 0.60)


def _stop_liquidity_score(record: StockRecord) -> Optional[float]:
    return _linear_score(record.dollar_volume_20d, 20_000_000.0, 250_000_000.0, higher_is_better=True)


def _stop_risk_score(record: StockRecord) -> Tuple[Optional[float], Dict[str, Optional[float]]]:
    drawdown = _stop_drawdown_score(record)
    balance_sheet = _stop_balance_sheet_score(record)
    volatility = _stop_volatility_score(record)
    earnings_stability = _stop_earnings_stability_score(record)
    liquidity = _stop_liquidity_score(record)
    risk_safety = weighted_average_available(
        {
            "drawdown": drawdown,
            "balance_sheet": balance_sheet,
            "volatility": volatility,
            "earnings_stability": earnings_stability,
            "liquidity_buffer": liquidity,
        },
        RISK_WEIGHTS["stop_checking_price"],
    )
    return risk_safety, {
        "drawdown": _safe_round(drawdown),
        "balance_sheet": _safe_round(balance_sheet),
        "volatility": _safe_round(volatility),
        "earnings_stability": _safe_round(earnings_stability),
        "liquidity_buffer": _safe_round(liquidity),
    }


def build_company_snapshot(
    record: StockRecord,
    confidence_score: float,
    confidence_label_text: str,
    data_quality_score: Optional[float] = None,
    data_quality_flags_list: Optional[List[str]] = None,
    normalization_notes_list: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "ticker": record.ticker,
        "company_name": record.company_name,
        "sector": record.sector,
        "industry": record.industry,
        "market_cap": record.market_cap,
        "price": record.price,
        "revenue_growth_yoy": record.revenue_growth_yoy,
        "eps_growth_yoy": record.eps_growth_yoy,
        "gross_margin_ttm": record.gross_margin_ttm if record.gross_margin_ttm is not None else record.gross_margin,
        "operating_margin_ttm": record.operating_margin_ttm if record.operating_margin_ttm is not None else record.operating_margin,
        "roe": record.roe if record.roe is not None else record.return_on_equity,
        "roic": record.roic,
        "free_cash_flow": record.free_cash_flow,
        "debt_to_equity_raw": record.debt_to_equity_raw if record.debt_to_equity_raw is not None else record.debt_to_equity,
        "debt_to_equity_normalized": _debt_to_equity_normalized(record),
        "debt_to_equity": _debt_to_equity_normalized(record),
        "shares_growth_yoy": record.shares_growth_yoy,
        "pe_ratio": record.pe_ratio,
        "ps_ratio": record.ps_ratio,
        "ev_to_ebitda": record.ev_to_ebitda,
        "max_drawdown_1y": record.max_drawdown_1y if record.max_drawdown_1y is not None else record.max_drawdown_252d,
        "volatility_1y": record.volatility_1y if record.volatility_1y is not None else record.volatility_63d,
        "price_vs_200dma": record.price_vs_200dma if record.price_vs_200dma is not None else record.price_vs_sma200_pct,
        "data_age_days": record.data_age_days,
        "price_data_age_days": _record_age_days(record, "price"),
        "fundamental_data_age_days": _record_age_days(record, "fundamental"),
        "shares_data_age_days": _record_age_days(record, "shares"),
        "financial_statement_date": record.financial_statement_date,
        "market_cap_timestamp": record.market_cap_timestamp,
        "data_quality_score": data_quality_score,
        "data_quality_flags": data_quality_flags_list or [],
        "normalization_notes": normalization_notes_list or [],
        "confidence_score": confidence_score,
        "confidence_label": confidence_label_text,
    }


STOP_ACTION_RANK = {
    "EXCLUDE": 0,
    "WATCHLIST_DATA_INSUFFICIENT": 1,
    "AVOID": 2,
    "WATCHLIST": 3,
    "WATCHLIST_HIGH_QUALITY": 4,
    "HOLD_OR_REVIEW": 5,
    "BUY_CANDIDATE": 6,
}


def _cap_stop_checking_price_action(action: str, max_action: Optional[str]) -> str:
    if max_action is None:
        return action
    if action not in STOP_ACTION_RANK or max_action not in STOP_ACTION_RANK:
        return action
    if STOP_ACTION_RANK[action] <= STOP_ACTION_RANK[max_action]:
        return action
    return max_action


def assign_stop_checking_price_action(
    score: float,
    confidence_score: float,
    is_rebalance_window_flag: bool,
    hard_excluded: bool,
    critical_missing: bool = False,
    max_action: Optional[str] = None,
) -> str:
    if hard_excluded:
        return "EXCLUDE"
    if confidence_score < 0.55:
        return "WATCHLIST_DATA_INSUFFICIENT"
    if not is_rebalance_window_flag:
        if score >= 75:
            action = "WATCHLIST_HIGH_QUALITY"
            return _cap_stop_checking_price_action(action, "WATCHLIST" if critical_missing else max_action)
        if score >= 65:
            action = "WATCHLIST"
            return _cap_stop_checking_price_action(action, "WATCHLIST" if critical_missing else max_action)
        action = "AVOID"
        return _cap_stop_checking_price_action(action, "WATCHLIST" if critical_missing else max_action)
    if score >= 82 and confidence_score >= 0.70:
        action = "BUY_CANDIDATE"
        return _cap_stop_checking_price_action(action, "WATCHLIST" if critical_missing else max_action)
    if score >= 72 and confidence_score >= 0.65:
        action = "HOLD_OR_REVIEW"
        return _cap_stop_checking_price_action(action, "WATCHLIST" if critical_missing else max_action)
    if score >= 62:
        action = "WATCHLIST"
        return _cap_stop_checking_price_action(action, "WATCHLIST" if critical_missing else max_action)
    action = "AVOID"
    return _cap_stop_checking_price_action(action, "WATCHLIST" if critical_missing else max_action)


def generate_stop_checking_price_reasons(record: StockRecord, scores: Dict[str, Optional[float]], penalties: List[Dict[str, Any]], confidence_score: float) -> List[str]:
    reasons: List[str] = []
    revenue_growth = _stop_ratio_field(record, "revenue_growth_yoy")
    eps_growth = _stop_ratio_field(record, "eps_growth_yoy")
    operating_margin_ttm = _stop_ratio_field(record, "operating_margin_ttm")
    free_cash_flow = _stop_field(record, "free_cash_flow")
    roe = _stop_ratio_field(record, "roe")
    roic = _stop_ratio_field(record, "roic")
    debt_to_equity = _debt_to_equity_normalized(record)
    shares_growth_yoy = _stop_ratio_field(record, "shares_growth_yoy")
    max_drawdown_1y = _stop_ratio_field(record, "max_drawdown_1y")
    price_vs_200dma = _stop_ratio_field(record, "price_vs_200dma")
    if price_vs_200dma is None and record.price is not None and record.ma_200 not in (None, 0):
        price_vs_200dma = record.price / record.ma_200 - 1.0

    if revenue_growth is not None and eps_growth is not None and revenue_growth > 0 and eps_growth > 0:
        reasons.append("營收與 EPS 維持正成長，基本面具延續性。")
    if operating_margin_ttm is not None and operating_margin_ttm >= 0.15:
        reasons.append("營業利益率健康，顯示公司具備獲利能力。")
    if free_cash_flow is not None and free_cash_flow > 0:
        reasons.append("自由現金流為正，獲利品質較佳。")
    if roe is not None and roic is not None and (roe >= 0.15 or roic >= 0.12):
        reasons.append("ROE / ROIC 表現良好，資本使用效率佳。")
    elif roic is not None and roic >= 0.12:
        reasons.append("ROIC 表現良好，資本使用效率佳。")
    elif roe is not None and roe >= 0.15:
        reasons.append("ROE 表現良好，股東權益報酬率具支撐。")
    if debt_to_equity is not None and debt_to_equity <= 1.0:
        reasons.append("負債水準可控，資產負債表風險較低。")
    if shares_growth_yoy is not None and shares_growth_yoy <= 0.02:
        reasons.append("股本稀釋有限，對每股價值較友善。")
    if max_drawdown_1y is not None and max_drawdown_1y >= -0.30:
        reasons.append("近一年回撤相對可控。")
    if price_vs_200dma is not None and price_vs_200dma >= 0:
        reasons.append("股價未跌破長期趨勢線，長期趨勢尚未明顯轉弱。")
    if not reasons and scores.get("fundamental") is not None:
        reasons.append("公司快照顯示仍具長期持有價值，但建議再做人工複查。")
    if confidence_score < 0.70:
        reasons.append("資料完整度不足，需人工複查。")
    if penalties:
        reasons.append("已套用風險懲罰，仍需留意基本面與回撤。")
    return reasons[:6]


def generate_stop_checking_price_risk_warnings(record: StockRecord, confidence_score: float) -> List[str]:
    warnings: List[str] = []
    free_cash_flow = _stop_field(record, "free_cash_flow")
    operating_margin_ttm = _stop_ratio_field(record, "operating_margin_ttm")
    debt_to_equity = _debt_to_equity_normalized(record)
    sector_aware = _is_sector_aware_debt_sector(record)
    shares_growth_yoy = _stop_ratio_field(record, "shares_growth_yoy")
    max_drawdown_1y = _stop_ratio_field(record, "max_drawdown_1y")
    pe_ratio = _stop_field(record, "pe_ratio")
    ps_ratio = _stop_field(record, "ps_ratio")
    data_age_days = _record_age_days(record, "price")
    if confidence_score < 0.70:
        warnings.append("資料完整度不足，需人工複查。")
    if free_cash_flow is not None and free_cash_flow < 0:
        warnings.append("自由現金流為負，獲利品質需進一步確認。")
    if operating_margin_ttm is not None and operating_margin_ttm < 0:
        warnings.append("營業利益率為負，本業獲利能力偏弱。")
    if debt_to_equity is not None:
        if sector_aware and debt_to_equity > 4:
            warnings.append("資本結構槓桿偏高，需與同業比較。")
        elif not sector_aware and debt_to_equity > 2:
            warnings.append("負債權益比偏高，景氣下行時風險較大。")
    if shares_growth_yoy is not None and shares_growth_yoy > 0.05:
        warnings.append("股本稀釋偏高，可能壓低每股價值。")
    if max_drawdown_1y is not None and max_drawdown_1y < -0.40:
        warnings.append("近一年最大回撤過深，波動承受度要求較高。")
    if (pe_ratio is not None and pe_ratio > 50) or (ps_ratio is not None and ps_ratio > 20):
        warnings.append("估值偏高，需確認成長能否支撐目前價格。")
    if data_age_days is not None and data_age_days > 3:
        warnings.append("價格資料不是最新，需確認資料時點。")
    return warnings


def _stop_confidence_notes(record: StockRecord, confidence_score: float) -> List[str]:
    notes: List[str] = []
    missing = [field for field in STOP_CHECKING_PRICE_REQUIRED_FIELDS if _get_field_value(record, field) in (None, "")]
    if missing:
        notes.append(f"缺少必要欄位: {', '.join(missing)}")
    if _get_field_value(record, "roic") in (None, ""):
        notes.append("ROIC 缺失，資本效率判斷主要依賴 ROE，需人工複查。")
    price_data_age_days = _record_age_days(record, "price")
    fundamental_data_age_days = _record_age_days(record, "fundamental")
    shares_data_age_days = _record_age_days(record, "shares")
    if price_data_age_days is not None and price_data_age_days > 3:
        notes.append(f"價格資料已 {price_data_age_days} 天未更新，判斷會比較舊")
    if fundamental_data_age_days is not None and fundamental_data_age_days > 30:
        notes.append(f"財報資料已 {fundamental_data_age_days} 天未更新，基本面判斷需複查")
    if shares_data_age_days is not None and shares_data_age_days > 30:
        notes.append(f"股本資料已 {shares_data_age_days} 天未更新，稀釋判斷需複查")
    if confidence_score < 0.70:
        notes.append("資料完整度偏低，建議人工複查。")
    if record.notes:
        notes.append(record.notes)
    return notes

def _reason_bullets(record: StockRecord, factor_scores: Dict[str, Optional[float]]) -> List[str]:
    reasons = []
    if factor_scores.get("growth") is not None and factor_scores["growth"] >= 65:
        reasons.append("成長動能不錯，營收與 EPS 具支撐")
    if factor_scores.get("quality") is not None and factor_scores["quality"] >= 65:
        reasons.append("獲利品質健康，毛利與營益率表現穩定")
    if factor_scores.get("valuation") is not None and factor_scores["valuation"] >= 60:
        reasons.append("估值未明顯過熱")
    if factor_scores.get("relative_strength") is not None and factor_scores["relative_strength"] >= 70:
        reasons.append("相對強勢領先同儕")
    if factor_scores.get("trend") is not None and factor_scores["trend"] >= 60:
        reasons.append("中長期趨勢仍偏多")
    if factor_scores.get("liquidity") is not None and factor_scores["liquidity"] >= 70:
        reasons.append("流動性充足，便於進出")
    if not reasons:
        reasons.append("整體分數仍具候選價值，可進一步人工審核")
    return reasons[:4]


def _is_fetch_failed_record(record: StockRecord) -> bool:
    raw = record.raw or {}
    nested_raw = raw.get("raw") if isinstance(raw.get("raw"), dict) else {}
    if raw.get("fetch_failed") is True or raw.get("fetch_status") == "failed":
        return True
    if nested_raw.get("fetch_failed") is True or nested_raw.get("fetch_status") == "failed":
        return True
    if record.notes and "抓取失敗" in record.notes:
        return True
    return False


def _is_retry_failed_record(record: StockRecord) -> bool:
    raw = record.raw or {}
    nested_raw = raw.get("raw") if isinstance(raw.get("raw"), dict) else {}
    return raw.get("retry_failed") is True or nested_raw.get("retry_failed") is True


def _missing_data_category(record: StockRecord, fallback: str) -> str:
    if _is_fetch_failed_record(record):
        return "fetch_failed_missing_data"
    return fallback


def _missing_data_reason(record: StockRecord, reason: str) -> str:
    if _is_fetch_failed_record(record):
        return f"抓取失敗造成缺資料；{reason}"
    return reason


def _record_age_days(record: StockRecord, category: str) -> Optional[int]:
    field_name = f"{category}_data_age_days"
    value = getattr(record, field_name, None)
    if value is not None:
        return value
    return record.data_age_days


def _record_raw_value(record: StockRecord, key: str) -> Any:
    raw = record.raw or {}
    nested_raw = raw.get("raw") if isinstance(raw.get("raw"), dict) else {}
    if key in raw:
        return raw.get(key)
    if isinstance(nested_raw, dict):
        return nested_raw.get(key)
    return None


def _price_history_points(record: StockRecord) -> Optional[int]:
    value = _coerce_float(_record_raw_value(record, "history_points"))
    if value is None:
        return None
    return int(value)


def _price_history_severity(record: StockRecord) -> Optional[str]:
    history_points = _price_history_points(record)
    if history_points is None or history_points >= 252:
        return None
    if history_points < 126:
        return "high"
    if history_points < 200:
        return "medium"
    return "low"


def normalization_notes(record: StockRecord) -> List[str]:
    notes: List[str] = []
    debt_raw = record.debt_to_equity_raw
    debt_normalized = _debt_to_equity_normalized(record)
    if debt_raw is not None and debt_normalized is not None and debt_raw != debt_normalized:
        notes.append(f"debt_to_equity 已由 raw {debt_raw} 正規化為 {debt_normalized:.2f}。")
    return notes


def data_quality_flags(record: StockRecord, confidence_score: Optional[float] = None) -> List[str]:
    flags: List[str] = []
    if _is_fetch_failed_record(record):
        flags.append("抓取失敗，資料缺口可能來自資料源而不是公司本身。")

    price_age = _record_age_days(record, "price")
    fundamental_age = _record_age_days(record, "fundamental")
    shares_age = _record_age_days(record, "shares")
    if price_age is not None and price_age > 3:
        flags.append(f"價格資料已 {price_age} 天未更新。")
    if fundamental_age is not None and fundamental_age > 30:
        flags.append(f"財報資料已 {fundamental_age} 天未更新。")
    if shares_age is not None and shares_age > 30:
        flags.append(f"股本資料已 {shares_age} 天未更新。")

    missing_strict = _stop_strict_action_cap_missing(record)
    if missing_strict:
        flags.append(f"缺少關鍵品質欄位：{', '.join(missing_strict)}。")
    if _stop_missing_roic_with_roe(record):
        flags.append("ROIC 缺失，資本效率以 ROE 替代，需人工複查。")
    elif _stop_missing_roic_without_roe(record):
        flags.append("ROIC 與 ROE 皆缺失，資本效率判斷不足。")
    if confidence_score is not None and confidence_score < 0.70:
        flags.append("資料完整度偏低。")

    history_points = _price_history_points(record)
    history_severity = _price_history_severity(record)
    if history_points is not None and history_severity in {"high", "medium"}:
        severity_label = "嚴重不足" if history_severity == "high" else "偏短"
        flags.append(f"價格歷史長度{severity_label}，目前約 {history_points} 筆。")

    return flags


def calculate_data_quality_score(record: StockRecord, confidence_score: Optional[float] = None) -> float:
    completeness = confidence_score
    if completeness is None:
        completeness = calculate_confidence_score(record, STOP_CHECKING_PRICE_REQUIRED_FIELDS)

    freshness_scores = [
        score_low_better(_record_age_days(record, "price"), 3, 30),
        score_low_better(_record_age_days(record, "fundamental"), 30, 180),
        score_low_better(_record_age_days(record, "shares"), 30, 180),
    ]
    available_freshness = [score for score in freshness_scores if score is not None]
    freshness = sum(available_freshness) / len(available_freshness) / 100.0 if available_freshness else 0.60

    flags = data_quality_flags(record, confidence_score)
    consistency = max(0.0, 1.0 - min(len(flags), 5) * 0.12)
    source_reliability = 0.30 if _is_fetch_failed_record(record) else (0.85 if _record_raw_value(record, "source") == "yfinance" else 1.0)

    score = (
        completeness * 0.40
        + freshness * 0.25
        + consistency * 0.20
        + source_reliability * 0.15
    )
    return max(0.0, min(1.0, score))


def _stop_action_cap_reason(record: StockRecord, confidence_score: float) -> Optional[str]:
    if confidence_score < 0.55:
        return "資料完整度低於 55%，動作限制為 WATCHLIST_DATA_INSUFFICIENT。"
    missing_strict = _stop_strict_action_cap_missing(record)
    if missing_strict:
        return f"缺少 {', '.join(missing_strict)}，Stop mode 不允許高於 WATCHLIST。"
    if _stop_missing_roic_with_roe(record):
        return "ROIC 缺失，但有 ROE 可替代；Stop mode 不允許高於 WATCHLIST_HIGH_QUALITY。"
    return None


def _hard_filter_detail(
    reason: str,
    *,
    category: str,
    field: str,
    raw_value: Any = None,
    normalized_value: Any = None,
    threshold: Any = None,
) -> Dict[str, Any]:
    return {
        "reason": reason,
        "category": category,
        "field": field,
        "raw_value": raw_value,
        "normalized_value": normalized_value,
        "threshold": threshold,
    }


def _hard_filter_details(record: StockRecord, config: ScreenConfig, strategy_mode: str) -> Optional[Dict[str, Any]]:
    strategy_mode = _normalize_strategy_mode(strategy_mode)
    if record.halted:
        return _hard_filter_detail("停牌，今天不納入候選", category="halted", field="halted", raw_value=record.halted, normalized_value=record.halted, threshold=False)
    if record.is_otc:
        return _hard_filter_detail("OTC 標的，直接排除", category="otc", field="is_otc", raw_value=record.is_otc, normalized_value=record.is_otc, threshold=False)
    if record.is_etf:
        return _hard_filter_detail("ETF，這不是普通股", category="etf", field="is_etf", raw_value=record.is_etf, normalized_value=record.is_etf, threshold=False)
    if record.is_adr:
        return _hard_filter_detail("ADR，先排除", category="adr", field="is_adr", raw_value=record.is_adr, normalized_value=record.is_adr, threshold=False)
    if record.price is None:
        return _hard_filter_detail(
            _missing_data_reason(record, f"股價 {_format_price(record.price)}，低於門檻 {_format_price(config.min_price)}"),
            category=_missing_data_category(record, "missing_price"),
            field="price",
            raw_value=record.price,
            normalized_value=record.price,
            threshold=config.min_price,
        )
    if record.price < config.min_price:
        return _hard_filter_detail(
            f"股價 {_format_price(record.price)}，低於門檻 {_format_price(config.min_price)}",
            category="min_price",
            field="price",
            raw_value=record.price,
            normalized_value=record.price,
            threshold=config.min_price,
        )
    if record.market_cap is None:
        return _hard_filter_detail(
            _missing_data_reason(record, f"市值 {_format_usd_billions(record.market_cap)}，低於門檻 {_format_usd_billions(config.min_market_cap)}"),
            category=_missing_data_category(record, "missing_market_cap"),
            field="market_cap",
            raw_value=record.market_cap,
            normalized_value=record.market_cap,
            threshold=config.min_market_cap,
        )
    if record.market_cap < config.min_market_cap:
        return _hard_filter_detail(
            f"市值 {_format_usd_billions(record.market_cap)}，低於門檻 {_format_usd_billions(config.min_market_cap)}",
            category="min_market_cap",
            field="market_cap",
            raw_value=record.market_cap,
            normalized_value=record.market_cap,
            threshold=config.min_market_cap,
        )
    dollar_volume = record.dollar_volume_20d
    if dollar_volume is None:
        return _hard_filter_detail(
            _missing_data_reason(record, f"20日均成交額 {_format_usd_millions(dollar_volume)}，低於門檻 {_format_usd_millions(config.min_dollar_volume_20d)}"),
            category=_missing_data_category(record, "missing_liquidity"),
            field="avg_dollar_volume_20d",
            raw_value=record.avg_dollar_volume_20d,
            normalized_value=dollar_volume,
            threshold=config.min_dollar_volume_20d,
        )
    if dollar_volume < config.min_dollar_volume_20d:
        return _hard_filter_detail(
            f"20日均成交額 {_format_usd_millions(dollar_volume)}，低於門檻 {_format_usd_millions(config.min_dollar_volume_20d)}",
            category="min_liquidity",
            field="avg_dollar_volume_20d",
            raw_value=record.avg_dollar_volume_20d,
            normalized_value=dollar_volume,
            threshold=config.min_dollar_volume_20d,
        )
    price_data_age_days = _record_age_days(record, "price")
    if price_data_age_days is not None and price_data_age_days > config.max_data_age_days:
        return _hard_filter_detail(
            f"價格資料已 {price_data_age_days} 天未更新，超過上限 {config.max_data_age_days} 天",
            category="stale_data",
            field="price_data_age_days",
            raw_value=price_data_age_days,
            normalized_value=price_data_age_days,
            threshold=config.max_data_age_days,
        )
    if record.security_type and "preferred" in record.security_type.lower():
        return _hard_filter_detail("非普通股（preferred stock）", category="security_type", field="security_type", raw_value=record.security_type, normalized_value=record.security_type, threshold="common_stock")
    if record.exchange and "OTC" in record.exchange.upper():
        return _hard_filter_detail("交易所顯示 OTC", category="otc_exchange", field="exchange", raw_value=record.exchange, normalized_value=record.exchange, threshold="non-OTC")
    if strategy_mode == "stop_checking_price":
        extra_details = apply_stop_checking_price_extra_filter_details(record)
        if extra_details:
            return extra_details[0]
    return None


def _hard_filter(record: StockRecord, config: ScreenConfig, strategy_mode: str) -> Optional[str]:
    detail = _hard_filter_details(record, config, strategy_mode)
    return None if detail is None else str(detail["reason"])


def _stop_critical_fields_missing(record: StockRecord) -> List[str]:
    critical_fields = ("free_cash_flow", "roic", "shares_growth_yoy")
    return [field for field in critical_fields if _get_field_value(record, field) in (None, "")]


def _stop_has_critical_missing(record: StockRecord) -> bool:
    return len(_stop_critical_fields_missing(record)) > 0


def _stop_strict_action_cap_missing(record: StockRecord) -> List[str]:
    strict_fields = ("free_cash_flow", "shares_growth_yoy")
    missing = [field for field in strict_fields if _get_field_value(record, field) in (None, "")]
    if _stop_missing_roic_without_roe(record):
        missing.append("roic")
    return missing


def _stop_missing_roic_with_roe(record: StockRecord) -> bool:
    return _get_field_value(record, "roic") in (None, "") and _get_field_value(record, "roe") not in (None, "")


def _stop_missing_roic_without_roe(record: StockRecord) -> bool:
    return _get_field_value(record, "roic") in (None, "") and _get_field_value(record, "roe") in (None, "")


def _stop_max_action(record: StockRecord, confidence_score: float) -> Optional[str]:
    if confidence_score < 0.55:
        return "WATCHLIST_DATA_INSUFFICIENT"
    if _stop_strict_action_cap_missing(record):
        return "WATCHLIST"
    if _stop_missing_roic_with_roe(record):
        return "WATCHLIST_HIGH_QUALITY"
    return None


def _dedupe_company_key(ticker: str) -> str:
    normalized = ticker.upper().replace("/", "-")
    return STOP_CHECKING_PRICE_DEDUPE_COMPANY_GROUPS.get(normalized, normalized)


def _dedupe_company_candidates(candidates: List[ScreenResult]) -> Tuple[List[ScreenResult], List[ScreenResult]]:
    grouped: Dict[str, List[ScreenResult]] = {}
    for item in candidates:
        grouped.setdefault(_dedupe_company_key(item.ticker), []).append(item)

    kept: List[ScreenResult] = []
    deduped: List[ScreenResult] = []
    for group_items in grouped.values():
        if len(group_items) == 1:
            kept.append(group_items[0])
            continue
        ordered = sorted(
            group_items,
            key=lambda item: (
                -(item.adjusted_score or item.total_score or -1.0),
                -(item.confidence_score or -1.0),
                item.ticker,
            ),
        )
        winner = ordered[0]
        kept.append(winner)
        for duplicate in ordered[1:]:
            duplicate.excluded_reason = f"同公司股權類別去重，保留 {winner.ticker}"
            duplicate.exclusion_reasons = [duplicate.excluded_reason]
            duplicate.exclusion_details = [
                {
                    "reason": duplicate.excluded_reason,
                    "category": "company_dedupe",
                    "field": "ticker",
                    "raw_value": duplicate.ticker,
                    "normalized_value": _dedupe_company_key(duplicate.ticker),
                    "threshold": f"keep_one_per_company:{winner.ticker}",
                }
            ]
            duplicate.hard_exclusion = False
            deduped.append(duplicate)
    return kept, deduped


def _dedupe_stop_checking_price_candidates(candidates: List[ScreenResult]) -> Tuple[List[ScreenResult], List[ScreenResult]]:
    return _dedupe_company_candidates(candidates)


def _average_result_score(candidates: Sequence[ScreenResult], attr_name: str) -> Optional[float]:
    values = [getattr(item, attr_name) for item in candidates if getattr(item, attr_name) is not None]
    if not values:
        return None
    return _safe_round(sum(values) / len(values))


def _diagnose_ranking_style(
    average_fundamental: Optional[float],
    average_momentum: Optional[float],
    average_risk: Optional[float],
) -> str:
    if average_fundamental is None or average_momentum is None:
        if average_risk is not None and average_risk >= 75:
            return "defensive"
        return "balanced"
    if average_momentum - average_fundamental >= 15:
        return "momentum_driven"
    if average_fundamental - average_momentum >= 15:
        return "quality_driven"
    if average_risk is not None and average_risk >= 75:
        return "defensive"
    return "balanced"


def _candidate_has_warning_keyword(item: ScreenResult, keyword: str) -> bool:
    return any(keyword in warning for warning in item.risk_warnings)


def _candidate_has_missing_data(item: ScreenResult) -> bool:
    if item.action_cap_reason and "缺少" in item.action_cap_reason:
        return True
    if any("缺少" in note or "資料完整度" in note for note in item.confidence_notes):
        return True
    return False


def build_ranking_diagnostics(candidates: Sequence[ScreenResult]) -> Dict[str, Any]:
    average_total = _average_result_score(candidates, "total_score")
    average_fundamental = _average_result_score(candidates, "fundamental_score")
    average_momentum = _average_result_score(candidates, "momentum_score")
    average_risk = _average_result_score(candidates, "risk_safety_score")
    return {
        "ranking_style": _diagnose_ranking_style(average_fundamental, average_momentum, average_risk),
        "top_n_average_total_score": average_total,
        "top_n_average_fundamental_score": average_fundamental,
        "top_n_average_momentum_score": average_momentum,
        "top_n_average_risk_safety_score": average_risk,
        "high_risk_candidate_count": sum(1 for item in candidates if item.risk_safety_score is not None and item.risk_safety_score < 40),
        "expensive_candidate_count": sum(1 for item in candidates if _candidate_has_warning_keyword(item, "估值")),
        "high_volatility_candidate_count": sum(1 for item in candidates if _candidate_has_warning_keyword(item, "波動")),
        "deep_drawdown_candidate_count": sum(1 for item in candidates if _candidate_has_warning_keyword(item, "回撤")),
        "missing_data_candidate_count": sum(1 for item in candidates if _candidate_has_missing_data(item)),
    }


def _sector_relative_field_value(record: StockRecord, field_name: str) -> Optional[float]:
    if field_name == "avg_dollar_volume_20d":
        return _valid_number(record.dollar_volume_20d)
    if field_name == "gross_margin":
        value = _pick({"gross_margin": record.gross_margin, "gross_margin_ttm": record.gross_margin_ttm}, "gross_margin", "gross_margin_ttm")
    elif field_name == "operating_margin":
        value = _pick(
            {"operating_margin": record.operating_margin, "operating_margin_ttm": record.operating_margin_ttm},
            "operating_margin",
            "operating_margin_ttm",
        )
    elif field_name == "return_on_equity":
        value = _pick({"return_on_equity": record.return_on_equity, "roe": record.roe}, "return_on_equity", "roe")
    elif field_name == "volatility_63d":
        value = _pick({"volatility_63d": record.volatility_63d, "volatility_1y": record.volatility_1y}, "volatility_63d", "volatility_1y")
    elif field_name == "beta":
        value = _pick({"beta": record.beta, "beta_1y": record.beta_1y}, "beta", "beta_1y")
    elif field_name == "max_drawdown_252d":
        value = _pick(
            {"max_drawdown_252d": record.max_drawdown_252d, "max_drawdown_1y": record.max_drawdown_1y},
            "max_drawdown_252d",
            "max_drawdown_1y",
        )
    else:
        value = _get_field_value(record, field_name)
    number = _valid_number(value)
    if number is None:
        return None
    if field_name.startswith("max_drawdown") and number < 0:
        return abs(number)
    return number


def _sector_relative_peer_provenance(item: ScreenResult, candidates: Sequence[ScreenResult]) -> Tuple[str, int, int, str]:
    universe_records = [candidate.record for candidate in candidates if candidate.record is not None]
    if item.record is None:
        return "universe_missing_metadata", 0, 0, "missing record; preview cannot use industry or sector peers."
    industry = (item.record.industry or "").strip()
    if industry:
        industry_records = [record for record in universe_records if (record.industry or "").strip() == industry]
        if len(industry_records) >= SECTOR_RELATIVE_MIN_PEERS:
            return "industry", len(industry_records), len(industry_records), f"industry peer count {len(industry_records)} >= {SECTOR_RELATIVE_MIN_PEERS}."
    sector = (item.record.sector or "").strip()
    if not sector:
        return "universe_missing_metadata", len(universe_records), 0, "missing sector or industry metadata; preview used universe peers."
    sector_records = [record for record in universe_records if (record.sector or "").strip() == sector]
    if len(sector_records) >= SECTOR_RELATIVE_MIN_PEERS:
        return "sector", len(sector_records), len(sector_records), f"sector peer count {len(sector_records)} >= {SECTOR_RELATIVE_MIN_PEERS}."
    return (
        "universe_insufficient_peers",
        len(universe_records),
        len(sector_records),
        f"industry/sector peer count {len(sector_records)} < {SECTOR_RELATIVE_MIN_PEERS}; preview used universe peers.",
    )


def _sector_relative_peer_records(item: ScreenResult, candidates: Sequence[ScreenResult]) -> Tuple[List[StockRecord], str, int]:
    universe_records = [candidate.record for candidate in candidates if candidate.record is not None]
    peer_source, _peer_count, sector_count, _peer_reason = _sector_relative_peer_provenance(item, candidates)
    if peer_source == "industry" and item.record is not None:
        industry = (item.record.industry or "").strip()
        return [record for record in universe_records if (record.industry or "").strip() == industry], peer_source, sector_count
    if peer_source == "sector" and item.record is not None:
        sector = (item.record.sector or "").strip()
        return [record for record in universe_records if (record.sector or "").strip() == sector], peer_source, sector_count
    return universe_records, peer_source, sector_count


def _sector_relative_factor_score(
    item: ScreenResult,
    candidates: Sequence[ScreenResult],
    field_name: str,
    direction: str,
) -> Tuple[Optional[float], List[str]]:
    notes: List[str] = []
    if item.record is None:
        return None, ["missing record; sector-relative preview unavailable."]
    value = _sector_relative_field_value(item.record, field_name)
    if value is None:
        return None, [f"missing sector-relative field: {field_name}"]
    peer_records, peer_source, sector_count = _sector_relative_peer_records(item, candidates)
    peer_values = [_sector_relative_field_value(record, field_name) for record in peer_records]
    valid_peer_values = [number for number in peer_values if number is not None]
    if not valid_peer_values:
        return None, [f"no valid peer values for {field_name}"]
    winsorized_peer_values = [
        number for number in winsorize_series(valid_peer_values, lower_pct=0.05, upper_pct=0.95)
        if number is not None
    ]
    if winsorized_peer_values:
        value = winsorize_value(value, min(winsorized_peer_values), max(winsorized_peer_values))
        valid_peer_values = winsorized_peer_values
    if peer_source == "universe_missing_metadata":
        notes.append("missing sector or industry metadata; used universe fallback.")
    elif peer_source == "universe_insufficient_peers":
        notes.append(f"sector peer count {sector_count} < {SECTOR_RELATIVE_MIN_PEERS}; used universe fallback.")
    if direction == "higher":
        score = score_higher_is_better(value, valid_peer_values, missing_policy="ignore")
    else:
        score = score_lower_is_better(value, valid_peer_values, missing_policy="ignore")
    return score, notes


def _average_equal_scores(scores: Sequence[Optional[float]]) -> Optional[float]:
    available = [score for score in scores if score is not None]
    if not available:
        return None
    return sum(available) / len(available)


def _sector_relative_scores_for_candidate(
    item: ScreenResult,
    candidates: Sequence[ScreenResult],
    strategy_mode: str,
) -> Tuple[Optional[float], Dict[str, Optional[float]], List[str]]:
    notes: List[str] = []
    note_seen = set()
    group_scores: Dict[str, Optional[float]] = {}
    for group_name, fields in SECTOR_RELATIVE_FACTOR_FIELDS.items():
        field_scores: List[Optional[float]] = []
        for field_name, direction in fields:
            score, field_notes = _sector_relative_factor_score(item, candidates, field_name, direction)
            field_scores.append(score)
            for note in field_notes:
                if note not in note_seen:
                    note_seen.add(note)
                    notes.append(note)
        group_scores[group_name] = _safe_round(_average_equal_scores(field_scores))

    fundamental = weighted_average_available(
        {
            "growth": group_scores["growth"],
            "quality": group_scores["quality"],
            "valuation": group_scores["valuation"],
            "capital_efficiency": None,
        },
        FUNDAMENTAL_WEIGHTS[strategy_mode],
    )
    risk_safety = group_scores["risk"]
    preview_score = weighted_average_available(
        {
            "fundamental": fundamental,
            "momentum": group_scores["momentum"],
            "risk_safety": risk_safety,
        },
        STRATEGY_WEIGHTS[strategy_mode],
    )
    factor_scores = {
        "growth": group_scores["growth"],
        "quality": group_scores["quality"],
        "valuation": group_scores["valuation"],
        "fundamental": _safe_round(fundamental),
        "momentum": group_scores["momentum"],
        "risk": risk_safety,
        "risk_safety": risk_safety,
    }
    if preview_score is None:
        notes.append("sector-aware preview unavailable: no valid sector-relative factors.")
    return _safe_round(preview_score), factor_scores, notes


def _sector_relative_mover(item: ScreenResult) -> Dict[str, Any]:
    return {
        "ticker": item.ticker,
        "rank_delta": item.sector_relative_rank_delta,
        "score_delta": item.sector_relative_score_delta,
        "preview_score": item.sector_relative_score_preview,
        "current_score": item.total_score,
        "preview_rank": item.sector_relative_rank_preview,
        "factor_scores": item.sector_relative_factor_scores,
        "notes": item.sector_relative_notes[:5],
        "peer_source": item.sector_relative_peer_source,
        "peer_count": item.sector_relative_peer_count,
        "peer_reason": item.sector_relative_peer_reason,
    }


def _pearson_correlation(pairs: Sequence[Tuple[float, float]]) -> Optional[float]:
    if len(pairs) < 2:
        return None
    left_values = [left for left, _right in pairs]
    right_values = [right for _left, right in pairs]
    left_mean = sum(left_values) / len(left_values)
    right_mean = sum(right_values) / len(right_values)
    numerator = sum((left - left_mean) * (right - right_mean) for left, right in pairs)
    left_variance = sum((left - left_mean) ** 2 for left in left_values)
    right_variance = sum((right - right_mean) ** 2 for right in right_values)
    denominator = math.sqrt(left_variance * right_variance)
    if denominator == 0:
        return None
    return numerator / denominator


def apply_sector_relative_preview(candidates: Sequence[ScreenResult], strategy_mode: str) -> Dict[str, Any]:
    strategy_mode = _normalize_strategy_mode(strategy_mode)
    current_ranks = {item.ticker: index for index, item in enumerate(candidates, start=1)}
    for item in candidates:
        peer_source, peer_count, _sector_count, peer_reason = _sector_relative_peer_provenance(item, candidates)
        item.sector_relative_peer_source = peer_source
        item.sector_relative_peer_count = peer_count
        item.sector_relative_peer_reason = peer_reason
        preview_score, factor_scores, notes = _sector_relative_scores_for_candidate(item, candidates, strategy_mode)
        item.sector_relative_score_preview = preview_score
        item.sector_relative_factor_scores = factor_scores
        item.sector_relative_notes = notes
        item.sector_relative_score_delta = None
        if preview_score is not None and item.total_score is not None:
            item.sector_relative_score_delta = _safe_round(preview_score - item.total_score)

    ranked_preview = sorted(
        [item for item in candidates if item.sector_relative_score_preview is not None],
        key=lambda item: (
            -(item.sector_relative_score_preview or -1.0),
            current_ranks.get(item.ticker, 10**9),
            item.ticker,
        ),
    )
    for preview_rank, item in enumerate(ranked_preview, start=1):
        item.sector_relative_rank_preview = preview_rank
        current_rank = current_ranks.get(item.ticker)
        item.sector_relative_rank_delta = None if current_rank is None else current_rank - preview_rank

    score_deltas = [item.sector_relative_score_delta for item in ranked_preview if item.sector_relative_score_delta is not None]
    rank_changed = [item for item in ranked_preview if item.sector_relative_rank_delta not in (None, 0)]
    top_overlap_total = min(10, len(candidates), len(ranked_preview))
    current_top = {item.ticker for item in candidates[:top_overlap_total]}
    preview_top = {item.ticker for item in ranked_preview[:top_overlap_total]}
    top_overlap = len(current_top & preview_top) if top_overlap_total else None
    score_pairs = [
        (item.total_score, item.sector_relative_score_preview)
        for item in ranked_preview
        if item.total_score is not None and item.sector_relative_score_preview is not None
    ]
    correlation = _pearson_correlation(score_pairs)  # type: ignore[arg-type]
    large_rank_changes = [
        item
        for item in ranked_preview
        if item.sector_relative_rank_delta is not None
        and abs(item.sector_relative_rank_delta) >= SECTOR_RELATIVE_LARGE_RANK_CHANGE_THRESHOLD
    ]
    largest_movers = sorted(
        rank_changed,
        key=lambda item: (
            -abs(item.sector_relative_rank_delta or 0),
            -abs(item.sector_relative_score_delta or 0),
            item.ticker,
        ),
    )
    movers_up = sorted(
        [item for item in ranked_preview if (item.sector_relative_rank_delta or 0) > 0],
        key=lambda item: (-(item.sector_relative_rank_delta or 0), item.ticker),
    )
    movers_down = sorted(
        [item for item in ranked_preview if (item.sector_relative_rank_delta or 0) < 0],
        key=lambda item: ((item.sector_relative_rank_delta or 0), item.ticker),
    )
    peer_counts = [item.sector_relative_peer_count for item in ranked_preview if item.sector_relative_peer_count is not None]
    return {
        "sector_aware_shadow_mode": True,
        "sector_aware_official_scoring": True,
        "sector_aware_preview_available_count": len(ranked_preview),
        "sector_aware_preview_missing_count": len(candidates) - len(ranked_preview),
        "sector_aware_average_score_delta": _safe_round(sum(score_deltas) / len(score_deltas)) if score_deltas else None,
        "sector_aware_rank_changed_count": len(rank_changed),
        "sector_aware_top_movers_up": [_sector_relative_mover(item) for item in movers_up[:5]],
        "sector_aware_top_movers_down": [_sector_relative_mover(item) for item in movers_down[:5]],
        "sector_aware_preview_coverage": _safe_round(len(ranked_preview) / len(candidates), 3) if candidates else None,
        "sector_aware_score_correlation_with_current": _safe_round(correlation, 3),
        "sector_aware_top_10_overlap": top_overlap,
        "sector_aware_top_10_overlap_total": top_overlap_total,
        "sector_aware_large_rank_change_count": len(large_rank_changes),
        "sector_aware_large_rank_change_threshold": SECTOR_RELATIVE_LARGE_RANK_CHANGE_THRESHOLD,
        "sector_aware_largest_movers": [_sector_relative_mover(item) for item in largest_movers[:10]],
        "sector_aware_sector_peer_used_count": sum(1 for item in ranked_preview if item.sector_relative_peer_source in {"industry", "sector"}),
        "sector_aware_industry_peer_used_count": sum(1 for item in ranked_preview if item.sector_relative_peer_source == "industry"),
        "sector_aware_sector_only_peer_used_count": sum(1 for item in ranked_preview if item.sector_relative_peer_source == "sector"),
        "sector_aware_universe_fallback_count": sum(1 for item in ranked_preview if item.sector_relative_peer_source == "universe_insufficient_peers"),
        "sector_aware_missing_sector_count": sum(1 for item in ranked_preview if item.sector_relative_peer_source == "universe_missing_metadata"),
        "sector_aware_universe_missing_metadata_count": sum(1 for item in ranked_preview if item.sector_relative_peer_source == "universe_missing_metadata"),
        "sector_aware_not_scored_disabled_count": sum(1 for item in ranked_preview if item.sector_relative_peer_source == "not_scored_sector_aware_disabled"),
        "sector_aware_average_peer_count": _safe_round(sum(peer_counts) / len(peer_counts)) if peer_counts else None,
        "sector_aware_min_peer_count": min(peer_counts) if peer_counts else None,
        "sector_aware_max_peer_count": max(peer_counts) if peer_counts else None,
    }


def _format_sector_relative_preview(item: ScreenResult) -> str:
    if item.sector_relative_score_preview is None:
        return ""
    score_delta = item.sector_relative_score_delta
    rank_delta = item.sector_relative_rank_delta
    delta_text = "" if score_delta is None else f"{score_delta:+.1f}"
    rank_text = "" if item.sector_relative_rank_preview is None else f"rank #{item.sector_relative_rank_preview}"
    rank_delta_text = "" if rank_delta is None else f"{rank_delta:+d}"
    if rank_text and rank_delta_text:
        return f"{item.sector_relative_score_preview:.1f} ({delta_text}), {rank_text} ({rank_delta_text})"
    return f"{item.sector_relative_score_preview:.1f} ({delta_text})"


def _format_sector_relative_peer_source(item: ScreenResult) -> str:
    source = item.sector_relative_peer_source
    if source == "industry":
        industry = (item.record.industry or "").strip() if item.record is not None else ""
        return f"{industry} industry" if industry else "Industry peers"
    if source == "sector":
        sector = (item.record.sector or "").strip() if item.record is not None else ""
        return f"{sector} sector" if sector else "Sector peers"
    if source == "universe_insufficient_peers":
        return "Universe fallback (insufficient peers)"
    if source == "universe_missing_metadata":
        return "Universe fallback (missing metadata)"
    if source == "not_scored_sector_aware_disabled":
        return "Not scored: sector-aware disabled"
    return ""


def _store_legacy_scores(item: ScreenResult) -> None:
    item.legacy_total_score = item.total_score
    item.legacy_raw_score = item.raw_score
    item.legacy_adjusted_score = item.adjusted_score
    item.legacy_fundamental_score = item.fundamental_score
    item.legacy_momentum_score = item.momentum_score
    item.legacy_risk_safety_score = item.risk_safety_score
    item.factor_scores["legacy_total_score"] = item.legacy_total_score
    item.factor_scores["legacy_raw_score"] = item.legacy_raw_score
    item.factor_scores["legacy_adjusted_score"] = item.legacy_adjusted_score
    item.factor_scores["legacy_fundamental_score"] = item.legacy_fundamental_score
    item.factor_scores["legacy_momentum_score"] = item.legacy_momentum_score
    item.factor_scores["legacy_risk_safety_score"] = item.legacy_risk_safety_score


def _metadata_flag(record: StockRecord, key: str) -> bool:
    raw = record.raw or {}
    nested_raw = raw.get("raw") if isinstance(raw.get("raw"), dict) else {}
    return raw.get(key) is True or (isinstance(nested_raw, dict) and nested_raw.get(key) is True)


def _metadata_coverage(records: Sequence[StockRecord]) -> Dict[str, Any]:
    total = len(records)
    sector_count = sum(1 for record in records if _clean_metadata_text(record.sector) is not None)
    industry_count = sum(1 for record in records if _clean_metadata_text(record.industry) is not None)
    return {
        "sector_metadata_coverage": _safe_round(sector_count / total, 3) if total else 0.0,
        "industry_metadata_coverage": _safe_round(industry_count / total, 3) if total else 0.0,
        "metadata_fetch_failed_count": sum(1 for record in records if _metadata_flag(record, "metadata_fetch_failed")),
        "metadata_missing_count": sum(1 for record in records if _metadata_flag(record, "metadata_missing")),
    }


def load_market_context(path: Path | str) -> Dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("market context sidecar must be a JSON object")
    return payload


def _configured_composite_weights(strategy_mode: str, config: ScreenConfig) -> Dict[str, float]:
    if strategy_mode == "hybrid":
        return {
            "fundamental": config.fundamental_weight,
            "momentum": config.momentum_weight,
            "risk_safety": config.risk_weight,
        }
    return dict(get_strategy_weights(strategy_mode))


def _effective_composite_weights(strategy_mode: str, regime: str, configured: Dict[str, float]) -> Dict[str, float]:
    effective = dict(configured)
    if strategy_mode == "hybrid":
        if regime == MARKET_REGIME_RISK_ON:
            effective["momentum"] += 0.05
            effective["risk_safety"] -= 0.05
        elif regime == MARKET_REGIME_RISK_OFF:
            effective["fundamental"] -= 0.05
            effective["momentum"] -= 0.05
            effective["risk_safety"] += 0.10
    elif strategy_mode == "stop_checking_price" and regime == MARKET_REGIME_RISK_OFF:
        effective["momentum"] -= 0.05
        effective["risk_safety"] += 0.05
    total = sum(effective.values())
    if abs(total - 1.0) > 1e-9:
        raise ValueError(f"effective composite weights must sum to 1.0, got {total}")
    return {key: _safe_round(value, 3) or 0.0 for key, value in effective.items()}


def _classify_market_regime(market_context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(market_context, dict):
        return {
            "market_regime": MARKET_REGIME_NEUTRAL,
            "market_regime_status": MARKET_REGIME_STATUS_INSUFFICIENT,
            "market_regime_signals": {},
            "market_context": {},
        }
    spy_close = _coerce_float(market_context.get("spy_close"))
    spy_sma200 = _coerce_float(market_context.get("spy_sma200"))
    qqq_close = _coerce_float(market_context.get("qqq_close"))
    qqq_sma200 = _coerce_float(market_context.get("qqq_sma200"))
    vix_close = _coerce_float(market_context.get("vix_close"))
    breadth = _coerce_float(market_context.get("breadth_above_200dma"))
    signals: Dict[str, str] = {}
    if spy_close is not None and spy_sma200 not in (None, 0):
        signals["spy"] = MARKET_REGIME_RISK_ON if spy_close > spy_sma200 else MARKET_REGIME_RISK_OFF
    else:
        signals["spy"] = "missing"
    if qqq_close is not None and qqq_sma200 not in (None, 0):
        signals["qqq"] = MARKET_REGIME_RISK_ON if qqq_close > qqq_sma200 else MARKET_REGIME_RISK_OFF
    else:
        signals["qqq"] = "missing"
    if vix_close is None:
        signals["vix"] = "missing"
    elif vix_close < 20:
        signals["vix"] = MARKET_REGIME_RISK_ON
    elif vix_close >= 25:
        signals["vix"] = MARKET_REGIME_RISK_OFF
    else:
        signals["vix"] = MARKET_REGIME_NEUTRAL
    if breadth is None:
        signals["breadth"] = "missing"
    elif breadth >= 0.60:
        signals["breadth"] = MARKET_REGIME_RISK_ON
    elif breadth <= 0.40:
        signals["breadth"] = MARKET_REGIME_RISK_OFF
    else:
        signals["breadth"] = MARKET_REGIME_NEUTRAL

    valid_signals = [value for value in signals.values() if value != "missing"]
    risk_on_count = sum(1 for value in valid_signals if value == MARKET_REGIME_RISK_ON)
    risk_off_count = sum(1 for value in valid_signals if value == MARKET_REGIME_RISK_OFF)
    if len(valid_signals) < 3:
        regime = MARKET_REGIME_NEUTRAL
        status = MARKET_REGIME_STATUS_INSUFFICIENT
    elif risk_on_count >= 3 and risk_off_count == 0:
        regime = MARKET_REGIME_RISK_ON
        status = MARKET_REGIME_STATUS_ENABLED
    elif risk_off_count >= 3 and risk_on_count == 0:
        regime = MARKET_REGIME_RISK_OFF
        status = MARKET_REGIME_STATUS_ENABLED
    else:
        regime = MARKET_REGIME_NEUTRAL
        status = MARKET_REGIME_STATUS_ENABLED
    return {
        "market_regime": regime,
        "market_regime_status": status,
        "market_regime_signals": signals,
        "market_context": {
            "as_of_date": market_context.get("as_of_date"),
            "spy_close": spy_close,
            "spy_sma200": _coerce_float(market_context.get("spy_sma200")),
            "qqq_close": qqq_close,
            "qqq_sma200": _coerce_float(market_context.get("qqq_sma200")),
            "vix_close": vix_close,
            "breadth_above_200dma": breadth,
            "breadth_eligible_count": _coerce_int(market_context.get("breadth_eligible_count")),
            "market_context_source": market_context.get("market_context_source"),
        },
    }


def _apply_market_regime_overlay(
    candidates: Sequence[ScreenResult],
    strategy_mode: str,
    configured_weights: Dict[str, float],
    effective_weights: Dict[str, float],
    market_regime: str,
    market_regime_status: str,
    *,
    as_of: Optional[date],
    force_rebalance: bool,
) -> None:
    review_flag = force_rebalance or is_rebalance_window(as_of)
    overlay_enabled = market_regime_status == MARKET_REGIME_STATUS_ENABLED and market_regime != MARKET_REGIME_NEUTRAL
    for item in candidates:
        item.base_total_score = item.total_score
        if not overlay_enabled or item.total_score is None:
            item.market_regime_score_delta = 0.0 if item.total_score is not None else None
            continue
        regime_raw_score = weighted_average_available(
            {
                "fundamental": item.fundamental_score,
                "momentum": item.momentum_score,
                "risk_safety": item.risk_safety_score,
            },
            effective_weights,
        )
        if regime_raw_score is None:
            item.market_regime_score_delta = 0.0 if item.base_total_score is not None else None
            continue
        if strategy_mode == "stop_checking_price":
            penalty_score = item.penalty_score or 0.0
            confidence_multiplier = item.confidence_multiplier or 1.0
            adjusted_before_confidence = max(0.0, regime_raw_score - penalty_score)
            adjusted_score = max(0.0, min(100.0, adjusted_before_confidence * confidence_multiplier))
            item.raw_score = _safe_round(regime_raw_score)
            item.adjusted_score = _safe_round(adjusted_score)
            item.final_score = _safe_round(adjusted_score)
            item.total_score = _safe_round(adjusted_score)
            item.factor_scores["raw_score"] = item.raw_score
            item.factor_scores["adjusted_score"] = item.adjusted_score
            item.factor_scores["final_score"] = item.final_score
            item.factor_scores["total"] = item.total_score
            if item.record is not None and item.confidence_score is not None:
                max_action = _stop_max_action(item.record, item.confidence_score)
                critical_missing = bool(_stop_strict_action_cap_missing(item.record))
                item.suggested_action = assign_stop_checking_price_action(
                    item.total_score or 0.0,
                    item.confidence_score,
                    review_flag,
                    False,
                    critical_missing,
                    max_action,
                )
        else:
            item.raw_score = _safe_round(regime_raw_score)
            item.adjusted_score = _safe_round(regime_raw_score)
            item.final_score = _safe_round(regime_raw_score)
            item.total_score = _safe_round(regime_raw_score)
            item.factor_scores["total"] = item.total_score
            if item.record is not None:
                item.suggested_action, item.action_cap_reason = assign_hybrid_action(
                    item.record,
                    item.total_score,
                    item.risk_safety_score,
                )
        if item.base_total_score is not None and item.total_score is not None:
            item.market_regime_score_delta = _safe_round(item.total_score - item.base_total_score)
        else:
            item.market_regime_score_delta = None

def _disable_official_sector_aware(candidates: Sequence[ScreenResult]) -> None:
    for item in candidates:
        if item.legacy_total_score is None:
            _store_legacy_scores(item)
        item.sector_relative_peer_source = "not_scored_sector_aware_disabled"
        item.sector_relative_peer_count = 0
        item.sector_relative_peer_reason = "official sector-aware scoring disabled because sector metadata coverage is below 90%."
        item.official_score_source = "legacy_metadata_gate"
        item.factor_scores["sector_aware_official_score"] = False


def _record_has_sector_metadata(record: Optional[StockRecord]) -> bool:
    if record is None:
        return False
    return _clean_metadata_text(record.sector) is not None and _clean_metadata_text(record.industry) is not None


def promote_sector_relative_scores(
    candidates: Sequence[ScreenResult],
    strategy_mode: str,
    *,
    config: Optional[ScreenConfig] = None,
    review_flag: bool = False,
) -> None:
    strategy_mode = _normalize_strategy_mode(strategy_mode)
    config = config or ScreenConfig()
    for item in candidates:
        if item.sector_relative_score_preview is None:
            continue
        if item.legacy_total_score is None:
            _store_legacy_scores(item)
        if not _record_has_sector_metadata(item.record):
            item.official_score_source = "legacy_missing_metadata"
            item.factor_scores["sector_aware_official_score"] = False
            continue
        factor_scores = item.sector_relative_factor_scores
        sector_raw_score = item.sector_relative_score_preview
        if strategy_mode == "hybrid":
            sector_raw_score = weighted_average_available(
                {
                    "fundamental": factor_scores.get("fundamental"),
                    "momentum": factor_scores.get("momentum"),
                    "risk_safety": factor_scores.get("risk_safety"),
                },
                {
                    "fundamental": config.fundamental_weight,
                    "momentum": config.momentum_weight,
                    "risk_safety": config.risk_weight,
                },
            )
            if sector_raw_score is None:
                sector_raw_score = item.sector_relative_score_preview
        item.fundamental_score = factor_scores.get("fundamental")
        item.momentum_score = factor_scores.get("momentum")
        item.risk_safety_score = factor_scores.get("risk_safety")
        item.factor_scores.update(
            {
                "sector_aware_official_score": True,
                "fundamental": item.fundamental_score,
                "growth": factor_scores.get("growth"),
                "quality": factor_scores.get("quality"),
                "valuation": factor_scores.get("valuation"),
                "momentum": item.momentum_score,
                "risk_safety": item.risk_safety_score,
            }
        )
        item.official_score_source = "sector_aware"
        if strategy_mode == "stop_checking_price":
            penalty_score = item.penalty_score or 0.0
            confidence_multiplier = item.confidence_multiplier or 1.0
            adjusted_score = max(0.0, min(100.0, max(0.0, sector_raw_score - penalty_score) * confidence_multiplier))
            item.raw_score = _safe_round(sector_raw_score)
            item.adjusted_score = _safe_round(adjusted_score)
            item.total_score = _safe_round(adjusted_score)
            item.final_score = _safe_round(adjusted_score)
            item.factor_scores.update(
                {
                    "raw_score": item.raw_score,
                    "adjusted_score": item.adjusted_score,
                    "final_score": item.final_score,
                    "penalty_score": item.penalty_score,
                    "confidence_multiplier": item.confidence_multiplier,
                    "total": item.total_score,
                }
            )
            if item.record is not None and item.confidence_score is not None:
                max_action = _stop_max_action(item.record, item.confidence_score)
                critical_missing = bool(_stop_strict_action_cap_missing(item.record))
                item.suggested_action = assign_stop_checking_price_action(
                    item.total_score or 0.0,
                    item.confidence_score,
                    review_flag,
                    False,
                    critical_missing,
                    max_action,
                )
                item.reasons = generate_stop_checking_price_reasons(item.record, item.factor_scores, item.penalties, item.confidence_score)
        else:
            item.raw_score = _safe_round(sector_raw_score)
            item.adjusted_score = _safe_round(sector_raw_score)
            item.total_score = _safe_round(sector_raw_score)
            item.final_score = _safe_round(sector_raw_score)
            item.penalty_score = 0.0
            item.confidence_multiplier = 1.0
            item.factor_scores["total"] = item.total_score
            if item.record is not None:
                item.suggested_action, item.action_cap_reason = assign_hybrid_action(
                    item.record,
                    item.total_score,
                    item.risk_safety_score,
                )
                item.reasons = _reason_bullets(item.record, item.factor_scores)


def score_record(
    record: StockRecord,
    config: ScreenConfig,
    strategy_mode: str = DEFAULT_STRATEGY_MODE,
    *,
    as_of: Optional[date] = None,
    force_rebalance: bool = False,
) -> ScreenResult:
    strategy_mode = _normalize_strategy_mode(strategy_mode)
    exclusion_detail = _hard_filter_details(record, config, strategy_mode)
    excluded_reason = None if exclusion_detail is None else str(exclusion_detail["reason"])
    exclusion_details = [] if exclusion_detail is None else [exclusion_detail]

    if strategy_mode == "stop_checking_price":
        fundamental, fundamental_parts = _stop_fundamental_score(record)
        momentum, momentum_parts = _stop_momentum_score(record)
        risk_safety, risk_parts = _stop_risk_score(record)
        confidence_score = calculate_confidence_score(record, STOP_CHECKING_PRICE_REQUIRED_FIELDS)
        confidence_label_text = confidence_label(confidence_score)
        data_quality_score = calculate_data_quality_score(record, confidence_score)
        data_quality_flags_list = data_quality_flags(record, confidence_score)
        normalization_notes_list = normalization_notes(record)
        penalties, penalty_points = calculate_stop_checking_price_penalties(record)
        penalty_score = min(MAX_STOP_CHECKING_PRICE_PENALTY, penalty_points)
        confidence_multiplier = 0.75 + 0.25 * confidence_score
        critical_missing = bool(_stop_strict_action_cap_missing(record))
        action_cap_reason = _stop_action_cap_reason(record, confidence_score)
        max_action = _stop_max_action(record, confidence_score)
        raw_score = weighted_average_available(
            {
                "fundamental": fundamental,
                "risk_safety": risk_safety,
                "momentum": momentum,
            },
            STRATEGY_WEIGHTS["stop_checking_price"],
        )
        raw_score = raw_score or 0.0
        adjusted_before_confidence = max(0.0, raw_score - penalty_score)
        adjusted_score = max(0.0, min(100.0, adjusted_before_confidence * confidence_multiplier))
        factor_scores: Dict[str, Optional[float]] = {
            "fundamental": _safe_round(fundamental),
            "quality": fundamental_parts["quality"],
            "growth": fundamental_parts["growth"],
            "valuation": fundamental_parts["valuation"],
            "capital_efficiency": fundamental_parts["capital_efficiency"],
            "momentum": _safe_round(momentum),
            "long_term_trend": momentum_parts["long_term_trend"],
            "relative_strength": momentum_parts["relative_strength"],
            "persistence": momentum_parts["persistence"],
            "risk_safety": _safe_round(risk_safety),
            "drawdown": risk_parts["drawdown"],
            "balance_sheet": risk_parts["balance_sheet"],
            "volatility": risk_parts["volatility"],
            "earnings_stability": risk_parts["earnings_stability"],
            "liquidity_buffer": risk_parts["liquidity_buffer"],
            "raw_score": _safe_round(raw_score),
            "adjusted_score": _safe_round(adjusted_score),
            "penalty_score": _safe_round(penalty_score),
            "confidence_score": _safe_round(confidence_score, 3),
            "confidence_multiplier": _safe_round(confidence_multiplier, 3),
            "data_quality_score": _safe_round(data_quality_score, 3),
            "final_score": _safe_round(adjusted_score),
            "critical_missing": critical_missing,
        }
        if excluded_reason is not None:
            return ScreenResult(
                ticker=record.ticker,
                strategy_mode=strategy_mode,
                total_score=None,
                raw_score=_safe_round(raw_score),
                adjusted_score=_safe_round(adjusted_score),
                fundamental_score=None,
                momentum_score=None,
                risk_safety_score=None,
                factor_scores=factor_scores,
                reasons=[],
                risk_warnings=generate_stop_checking_price_risk_warnings(record, confidence_score),
                confidence_notes=_stop_confidence_notes(record, confidence_score),
                penalties=penalties,
                confidence_score=_safe_round(confidence_score, 3),
                confidence_label=confidence_label_text,
                company_snapshot=build_company_snapshot(
                    record,
                    confidence_score,
                    confidence_label_text,
                    _safe_round(data_quality_score, 3),
                    data_quality_flags_list,
                    normalization_notes_list,
                ),
                suggested_action="EXCLUDE",
                hard_exclusion=True,
                excluded_reason=excluded_reason,
                exclusion_reasons=[excluded_reason],
                exclusion_details=exclusion_details,
                record=record,
                penalty_score=_safe_round(penalty_score),
                confidence_multiplier=_safe_round(confidence_multiplier, 3),
                final_score=_safe_round(adjusted_score),
                data_quality_score=_safe_round(data_quality_score, 3),
                data_quality_flags=data_quality_flags_list,
                normalization_notes=normalization_notes_list,
                action_cap_reason=action_cap_reason,
            )
        review_flag = is_rebalance_window(as_of) or force_rebalance
        total_score = _safe_round(adjusted_score)
        company_snapshot = build_company_snapshot(
            record,
            confidence_score,
            confidence_label_text,
            _safe_round(data_quality_score, 3),
            data_quality_flags_list,
            normalization_notes_list,
        )
        suggested_action = assign_stop_checking_price_action(
            total_score or 0.0,
            confidence_score,
            review_flag,
            False,
            critical_missing,
            max_action,
        )
        return ScreenResult(
            ticker=record.ticker,
            strategy_mode=strategy_mode,
            total_score=total_score,
            raw_score=_safe_round(raw_score),
            adjusted_score=_safe_round(adjusted_score),
            fundamental_score=_safe_round(fundamental),
            momentum_score=_safe_round(momentum),
            risk_safety_score=_safe_round(risk_safety),
            factor_scores=factor_scores,
            reasons=generate_stop_checking_price_reasons(record, factor_scores, penalties, confidence_score),
            risk_warnings=generate_stop_checking_price_risk_warnings(record, confidence_score),
            confidence_notes=_stop_confidence_notes(record, confidence_score),
            penalties=penalties,
            confidence_score=_safe_round(confidence_score, 3),
            confidence_label=confidence_label_text,
            company_snapshot=company_snapshot,
            suggested_action=suggested_action,
            hard_exclusion=False,
            excluded_reason=None,
            exclusion_reasons=[],
            record=record,
            penalty_score=_safe_round(penalty_score),
            confidence_multiplier=_safe_round(confidence_multiplier, 3),
            final_score=_safe_round(adjusted_score),
            data_quality_score=_safe_round(data_quality_score, 3),
            data_quality_flags=data_quality_flags_list,
            normalization_notes=normalization_notes_list,
            action_cap_reason=action_cap_reason,
            official_score_source=None,
        )

    fundamental, fundamental_parts = _fundamental_score(record)
    momentum, momentum_parts = _momentum_score(record)
    risk_safety, risk_parts = _risk_score(record)

    factor_scores = {
        "fundamental": _safe_round(fundamental),
        "growth": fundamental_parts["growth"],
        "quality": fundamental_parts["quality"],
        "valuation": fundamental_parts["valuation"],
        "momentum": _safe_round(momentum),
        "relative_strength": momentum_parts["relative_strength"],
        "trend": momentum_parts["trend"],
        "persistence": momentum_parts["persistence"],
        "risk_safety": _safe_round(risk_safety),
        "volatility": risk_parts["volatility"],
        "beta": risk_parts["beta"],
        "drawdown": risk_parts["drawdown"],
        "liquidity": risk_parts["liquidity"],
    }

    if excluded_reason is not None:
        return ScreenResult(
            ticker=record.ticker,
            strategy_mode=strategy_mode,
            total_score=None,
            raw_score=None,
            adjusted_score=None,
            fundamental_score=None,
            momentum_score=None,
            risk_safety_score=None,
            factor_scores=factor_scores,
            reasons=[],
            risk_warnings=_risk_warnings(record),
            confidence_notes=_confidence_notes(record, factor_scores),
            penalties=[],
            confidence_score=None,
            confidence_label=None,
            company_snapshot=None,
            suggested_action="EXCLUDE",
            hard_exclusion=True,
            excluded_reason=excluded_reason,
            exclusion_reasons=[excluded_reason],
            exclusion_details=exclusion_details,
            record=record,
            official_score_source=None,
        )

    total = _average(
        _compact(
            [
                (fundamental, config.fundamental_weight) if fundamental is not None else None,
                (momentum, config.momentum_weight) if momentum is not None else None,
                (risk_safety, config.risk_weight) if risk_safety is not None else None,
            ]
        )
    )
    total_score = _safe_round(total)
    factor_scores["total"] = total_score
    suggested_action, action_cap_reason = assign_hybrid_action(record, total_score, _safe_round(risk_safety))
    return ScreenResult(
        ticker=record.ticker,
        strategy_mode=strategy_mode,
        total_score=total_score,
        raw_score=total_score,
        adjusted_score=total_score,
        fundamental_score=_safe_round(fundamental),
        momentum_score=_safe_round(momentum),
        risk_safety_score=_safe_round(risk_safety),
        factor_scores=factor_scores,
        reasons=_reason_bullets(record, factor_scores),
        risk_warnings=_risk_warnings(record),
        confidence_notes=_confidence_notes(record, factor_scores),
        penalties=[],
        confidence_score=None,
        confidence_label=None,
        company_snapshot=None,
        suggested_action=suggested_action,
        hard_exclusion=False,
        excluded_reason=None,
        exclusion_reasons=[],
        record=record,
        penalty_score=0.0,
        confidence_multiplier=1.0,
        final_score=total_score,
        action_cap_reason=action_cap_reason,
        official_score_source=None,
    )


def screen_records(
    records: Iterable[StockRecord],
    config: Optional[ScreenConfig] = None,
    top_n: Optional[int] = None,
    *,
    strategy_mode: str = DEFAULT_STRATEGY_MODE,
    as_of: Optional[date] = None,
    force_rebalance: bool = False,
    min_score: Optional[float] = None,
    market_context: Optional[Dict[str, Any]] = None,
) -> ScreeningReport:
    config = config or ScreenConfig()
    strategy_mode = _normalize_strategy_mode(strategy_mode)
    effective_min_score, effective_min_score_source = _resolve_effective_min_score(strategy_mode, min_score)
    review_flag = force_rebalance or is_rebalance_window(as_of)
    review_mode = "quarterly_rebalance" if strategy_mode == "stop_checking_price" and review_flag else "watchlist_only"
    scored = [
        score_record(record, config, strategy_mode=strategy_mode, as_of=as_of, force_rebalance=force_rebalance)
        for record in records
    ]
    scored_records = [item.record for item in scored if item.record is not None]
    metadata_summary = _metadata_coverage(scored_records)
    sector_aware_enabled = (metadata_summary["sector_metadata_coverage"] or 0.0) >= SECTOR_METADATA_COVERAGE_GATE
    sector_aware_status = "enabled" if sector_aware_enabled else "disabled_insufficient_sector_metadata"
    configured_composite_weights = _configured_composite_weights(strategy_mode, config)
    market_regime_summary = _classify_market_regime(market_context)
    effective_composite_weights = _effective_composite_weights(
        strategy_mode,
        market_regime_summary["market_regime"],
        configured_composite_weights,
    )
    candidates = [item for item in scored if item.is_candidate]
    excluded = [item for item in scored if not item.is_candidate]
    hard_pass_count = len(candidates)
    dedupe_company_enabled = strategy_mode in VALID_STRATEGY_MODES

    candidates.sort(
        key=lambda item: (
            -(item.adjusted_score or item.total_score or -1.0),
            -(item.factor_scores.get("momentum") or -1.0),
            item.ticker,
        )
    )
    sector_relative_summary = apply_sector_relative_preview(candidates, strategy_mode)
    if sector_aware_enabled:
        promote_sector_relative_scores(candidates, strategy_mode, config=config, review_flag=review_flag)
    else:
        _disable_official_sector_aware(candidates)
    _apply_market_regime_overlay(
        candidates,
        strategy_mode,
        configured_composite_weights,
        effective_composite_weights,
        market_regime_summary["market_regime"],
        market_regime_summary["market_regime_status"],
        as_of=as_of,
        force_rebalance=force_rebalance,
    )

    if strategy_mode == "stop_checking_price":
        candidates.sort(
            key=lambda item: (
                -(item.adjusted_score or item.total_score or -1.0),
                -(item.confidence_score or -1.0),
                item.ticker,
            )
        )
        if dedupe_company_enabled:
            candidates, deduped = _dedupe_company_candidates(candidates)
            excluded = excluded + deduped
        else:
            deduped = []
        candidates.sort(
            key=lambda item: (
                -(item.adjusted_score or item.total_score or -1.0),
                -(item.confidence_score or -1.0),
                item.ticker,
            )
        )
    else:
        candidates.sort(
            key=lambda item: (
                -(item.total_score or -1.0),
                -(item.factor_scores.get("momentum") or -1.0),
                item.ticker,
            )
        )
        if dedupe_company_enabled:
            candidates, deduped = _dedupe_company_candidates(candidates)
            excluded = excluded + deduped
            candidates.sort(
                key=lambda item: (
                    -(item.total_score or -1.0),
                    -(item.factor_scores.get("momentum") or -1.0),
                    item.ticker,
                )
            )
        else:
            deduped = []
    if top_n is None:
        top_n = config.top_n
    if effective_min_score is not None:
        candidates = [
            item
            for item in candidates
            if item.total_score is not None and item.total_score >= effective_min_score
        ]
    displayed_candidates = candidates[:top_n]
    ranking_diagnostics = build_ranking_diagnostics(displayed_candidates)
    soft_penalties: List[Dict[str, Any]] = []
    missing_data_warnings: List[Dict[str, Any]] = []
    if strategy_mode == "stop_checking_price":
        for item in displayed_candidates:
            penalty_score = item.penalty_score or 0.0
            if penalty_score > 0:
                soft_penalties.append(
                    {
                        "ticker": item.ticker,
                        "penalty_score": _safe_round(penalty_score),
                        "final_score": item.final_score,
                        "confidence_score": item.confidence_score,
                        "reasons": [penalty.get("reason") for penalty in item.penalties],
                    }
                )
            if item.record is not None and (_stop_has_critical_missing(item.record) or (item.confidence_score is not None and item.confidence_score < 1.0)):
                missing_data_warnings.append(
                    {
                        "ticker": item.ticker,
                        "confidence_score": item.confidence_score,
                        "confidence_label": item.confidence_label,
                        "data_quality_score": item.data_quality_score,
                        "data_quality_flags": item.data_quality_flags,
                        "action_cap_reason": item.action_cap_reason,
                        "missing_fields": _stop_critical_fields_missing(item.record),
                        "notes": item.confidence_notes,
                    }
                )
    return ScreeningReport(
        as_of=as_of,
        config=config,
        strategy_mode=strategy_mode,
        review_mode=review_mode,
        hard_pass_count=hard_pass_count,
        candidates=displayed_candidates,
        excluded=excluded,
        universe_size=len(scored),
        hard_excluded=[item for item in excluded if item.hard_exclusion is not False],
        soft_penalties=soft_penalties,
        missing_data_warnings=missing_data_warnings,
        min_score=effective_min_score,
        top_n=top_n,
        dedupe_company=dedupe_company_enabled,
        deduped=deduped,
        retry_failed_count=sum(1 for item in scored if item.record is not None and _is_retry_failed_record(item.record)),
        fetch_failed_count=sum(1 for item in scored if item.record is not None and _is_fetch_failed_record(item.record)),
        dedupe_removed_count=len(deduped),
        effective_min_score_source=effective_min_score_source,
        ranking_style=ranking_diagnostics["ranking_style"],
        top_n_average_total_score=ranking_diagnostics["top_n_average_total_score"],
        top_n_average_fundamental_score=ranking_diagnostics["top_n_average_fundamental_score"],
        top_n_average_momentum_score=ranking_diagnostics["top_n_average_momentum_score"],
        top_n_average_risk_safety_score=ranking_diagnostics["top_n_average_risk_safety_score"],
        high_risk_candidate_count=ranking_diagnostics["high_risk_candidate_count"],
        expensive_candidate_count=ranking_diagnostics["expensive_candidate_count"],
        high_volatility_candidate_count=ranking_diagnostics["high_volatility_candidate_count"],
        deep_drawdown_candidate_count=ranking_diagnostics["deep_drawdown_candidate_count"],
        missing_data_candidate_count=ranking_diagnostics["missing_data_candidate_count"],
        sector_aware_official_scoring=sector_aware_enabled,
        sector_aware_status=sector_aware_status,
        sector_metadata_coverage=metadata_summary["sector_metadata_coverage"],
        industry_metadata_coverage=metadata_summary["industry_metadata_coverage"],
        metadata_fetch_failed_count=metadata_summary["metadata_fetch_failed_count"],
        metadata_missing_count=metadata_summary["metadata_missing_count"],
        sector_aware_shadow_mode=sector_relative_summary["sector_aware_shadow_mode"],
        sector_aware_preview_available_count=sector_relative_summary["sector_aware_preview_available_count"],
        sector_aware_preview_missing_count=sector_relative_summary["sector_aware_preview_missing_count"],
        sector_aware_average_score_delta=sector_relative_summary["sector_aware_average_score_delta"],
        sector_aware_rank_changed_count=sector_relative_summary["sector_aware_rank_changed_count"],
        sector_aware_top_movers_up=sector_relative_summary["sector_aware_top_movers_up"],
        sector_aware_top_movers_down=sector_relative_summary["sector_aware_top_movers_down"],
        sector_aware_preview_coverage=sector_relative_summary["sector_aware_preview_coverage"],
        sector_aware_score_correlation_with_current=sector_relative_summary["sector_aware_score_correlation_with_current"],
        sector_aware_top_10_overlap=sector_relative_summary["sector_aware_top_10_overlap"],
        sector_aware_top_10_overlap_total=sector_relative_summary["sector_aware_top_10_overlap_total"],
        sector_aware_large_rank_change_count=sector_relative_summary["sector_aware_large_rank_change_count"],
        sector_aware_large_rank_change_threshold=sector_relative_summary["sector_aware_large_rank_change_threshold"],
        sector_aware_largest_movers=sector_relative_summary["sector_aware_largest_movers"],
        sector_aware_sector_peer_used_count=0 if not sector_aware_enabled else sector_relative_summary["sector_aware_sector_peer_used_count"],
        sector_aware_industry_peer_used_count=0 if not sector_aware_enabled else sector_relative_summary["sector_aware_industry_peer_used_count"],
        sector_aware_sector_only_peer_used_count=0 if not sector_aware_enabled else sector_relative_summary["sector_aware_sector_only_peer_used_count"],
        sector_aware_universe_fallback_count=0 if not sector_aware_enabled else sector_relative_summary["sector_aware_universe_fallback_count"],
        sector_aware_missing_sector_count=0 if not sector_aware_enabled else sector_relative_summary["sector_aware_missing_sector_count"],
        sector_aware_universe_missing_metadata_count=0 if not sector_aware_enabled else sector_relative_summary["sector_aware_universe_missing_metadata_count"],
        sector_aware_not_scored_disabled_count=len(displayed_candidates) if not sector_aware_enabled else 0,
        sector_aware_average_peer_count=sector_relative_summary["sector_aware_average_peer_count"],
        sector_aware_min_peer_count=sector_relative_summary["sector_aware_min_peer_count"],
        sector_aware_max_peer_count=sector_relative_summary["sector_aware_max_peer_count"],
        market_context=market_regime_summary["market_context"],
        configured_composite_weights=configured_composite_weights,
        effective_composite_weights=effective_composite_weights,
        market_regime=market_regime_summary["market_regime"],
        market_regime_status=market_regime_summary["market_regime_status"],
        market_regime_signals=market_regime_summary["market_regime_signals"],
    )


def _render_markdown(report: ScreeningReport) -> str:
    lines = []
    lines.append("# US 股票候選清單")
    lines.append("")
    lines.append(f"- 策略模式：{report.strategy_mode}")
    lines.append(f"- 檢查模式：{report.review_mode}")
    lines.append("")
    lines.append(f"- 輸入 {report.universe_size} 檔")
    lines.append(f"- min_score：{report.min_score if report.min_score is not None else '未設定'}")
    lines.append(f"- effective_min_score_source：{report.effective_min_score_source}")
    lines.append(f"- top_n：{report.top_n}")
    lines.append(f"- dedupe_company：{'啟用' if report.dedupe_company else '未啟用'}")
    lines.append(f"- 硬篩通過 {report.hard_pass_count} 檔")
    lines.append(f"- 顯示前 {len(report.candidates)} 名")
    lines.append(f"- hard_exclusion count：{len(report.hard_excluded)}")
    lines.append(f"- soft_penalty count：{len(report.soft_penalties)}")
    lines.append(f"- missing_data count：{len(report.missing_data_warnings)}")
    lines.append(f"- retry_failed_count：{report.retry_failed_count}")
    lines.append(f"- fetch_failed_count：{report.fetch_failed_count}")
    lines.append(f"- dedupe_removed_count：{report.dedupe_removed_count}")
    lines.append(f"- ranking_style：{report.ranking_style}")
    lines.append(f"- top_n_average_total_score：{report.top_n_average_total_score}")
    lines.append(f"- top_n_average_fundamental_score：{report.top_n_average_fundamental_score}")
    lines.append(f"- top_n_average_momentum_score：{report.top_n_average_momentum_score}")
    lines.append(f"- top_n_average_risk_safety_score：{report.top_n_average_risk_safety_score}")
    lines.append(f"- high_risk_candidate_count：{report.high_risk_candidate_count}")
    lines.append(f"- expensive_candidate_count：{report.expensive_candidate_count}")
    lines.append(f"- high_volatility_candidate_count：{report.high_volatility_candidate_count}")
    lines.append(f"- deep_drawdown_candidate_count：{report.deep_drawdown_candidate_count}")
    lines.append(f"- missing_data_candidate_count：{report.missing_data_candidate_count}")
    lines.append(f"- sector_metadata_coverage：{report.sector_metadata_coverage}")
    lines.append(f"- industry_metadata_coverage：{report.industry_metadata_coverage}")
    lines.append(f"- metadata_fetch_failed_count：{report.metadata_fetch_failed_count}")
    lines.append(f"- metadata_missing_count：{report.metadata_missing_count}")
    lines.append(f"- sector_aware_status：{report.sector_aware_status}")
    lines.append(f"- market_regime：{report.market_regime}")
    lines.append(f"- market_regime_status：{report.market_regime_status}")
    lines.append(f"- market_regime_signals：{json.dumps(report.market_regime_signals, ensure_ascii=False)}")
    lines.append(f"- configured_composite_weights：{json.dumps(report.configured_composite_weights, ensure_ascii=False)}")
    lines.append(f"- effective_composite_weights：{json.dumps(report.effective_composite_weights, ensure_ascii=False)}")
    lines.append(f"- market_context：{json.dumps(report.market_context, ensure_ascii=False)}")
    lines.append(f"- sector_aware_official_scoring：{'啟用' if report.sector_aware_official_scoring else '未啟用'}")
    lines.append(f"- sector_aware_shadow_mode：{'啟用' if report.sector_aware_shadow_mode else '未啟用'}")
    lines.append(f"- sector_aware_preview_available_count：{report.sector_aware_preview_available_count}")
    lines.append(f"- sector_aware_preview_missing_count：{report.sector_aware_preview_missing_count}")
    lines.append(f"- sector_aware_average_score_delta：{report.sector_aware_average_score_delta}")
    lines.append(f"- sector_aware_rank_changed_count：{report.sector_aware_rank_changed_count}")
    lines.append(f"- sector_aware_preview_coverage：{report.sector_aware_preview_coverage}")
    lines.append(f"- sector_aware_score_correlation_with_current：{report.sector_aware_score_correlation_with_current}")
    lines.append(f"- sector_aware_top_10_overlap：{report.sector_aware_top_10_overlap} / {report.sector_aware_top_10_overlap_total}")
    lines.append(f"- sector_aware_industry_peer_used_count：{report.sector_aware_industry_peer_used_count}")
    lines.append(f"- sector_aware_sector_only_peer_used_count：{report.sector_aware_sector_only_peer_used_count}")
    lines.append(f"- sector_aware_sector_peer_used_count：{report.sector_aware_sector_peer_used_count}")
    lines.append(f"- sector_aware_universe_fallback_count：{report.sector_aware_universe_fallback_count}")
    lines.append(f"- sector_aware_universe_missing_metadata_count：{report.sector_aware_universe_missing_metadata_count}")
    lines.append(f"- sector_aware_not_scored_disabled_count：{report.sector_aware_not_scored_disabled_count}")
    lines.append(f"- sector_aware_average_peer_count：{report.sector_aware_average_peer_count}")
    lines.append(f"- sector_aware_min_peer_count：{report.sector_aware_min_peer_count}")
    lines.append(f"- sector_aware_max_peer_count：{report.sector_aware_max_peer_count}")
    lines.append(
        f"- sector_aware_large_rank_change_count：{report.sector_aware_large_rank_change_count}"
        f"（threshold {report.sector_aware_large_rank_change_threshold}）"
    )
    if report.sector_aware_top_movers_up:
        movers = "；".join(
            f"{item['ticker']} rank_delta {item.get('rank_delta')} score_delta {item.get('score_delta')}"
            for item in report.sector_aware_top_movers_up
        )
        lines.append(f"- sector_aware_top_movers_up：{movers}")
    if report.sector_aware_top_movers_down:
        movers = "；".join(
            f"{item['ticker']} rank_delta {item.get('rank_delta')} score_delta {item.get('score_delta')}"
            for item in report.sector_aware_top_movers_down
        )
        lines.append(f"- sector_aware_top_movers_down：{movers}")
    if report.sector_aware_largest_movers:
        movers = "；".join(
            f"{item['ticker']} rank_delta {item.get('rank_delta')} score_delta {item.get('score_delta')}"
            for item in report.sector_aware_largest_movers[:5]
        )
        lines.append(f"- sector_aware_largest_movers：{movers}")
    if report.strategy_mode == "hybrid" and report.ranking_style == "momentum_driven":
        lines.append("- 診斷提醒：本次 hybrid 排名偏動量導向，適合作為候選初篩，不代表低風險或長期品質排序。")
    lines.append("")
    lines.append("## 候選名單")
    lines.append("")
    lines.append("| 排名 | Ticker | 總分 | Base score | Regime delta | Official source | Legacy score | Sector-aware preview | Peer source | Peer count | Peer reason | 資料品質 | 動作 | 基本面 | 動量 | 風險安全 | 主要理由 | 風險警示 |")
    lines.append("| --- | --- | ---: | ---: | ---: | --- | ---: | --- | --- | ---: | --- | ---: | --- | ---: | ---: | ---: | --- | --- |")
    for index, item in enumerate(report.candidates, start=1):
        warnings = "；".join(item.risk_warnings) if item.risk_warnings else "無"
        reasons = "；".join(item.reasons)
        lines.append(
            "| {rank} | {ticker} | {total} | {base_total} | {regime_delta} | {official_source} | {legacy_total} | {sector_preview} | {peer_source} | {peer_count} | {peer_reason} | {data_quality} | {action} | {fundamental} | {momentum} | {risk} | {reasons} | {warnings} |".format(
                rank=index,
                ticker=item.ticker,
                total=item.total_score if item.total_score is not None else "",
                base_total=item.base_total_score if item.base_total_score is not None else "",
                regime_delta=item.market_regime_score_delta if item.market_regime_score_delta is not None else "",
                official_source=item.official_score_source or "",
                legacy_total=item.legacy_total_score if item.legacy_total_score is not None else "",
                sector_preview=_format_sector_relative_preview(item),
                peer_source=_format_sector_relative_peer_source(item),
                peer_count=item.sector_relative_peer_count if item.sector_relative_peer_count is not None else "",
                peer_reason=item.sector_relative_peer_reason or "",
                data_quality=item.data_quality_score if item.data_quality_score is not None else "",
                action=item.suggested_action or "",
                fundamental=item.factor_scores.get("fundamental") or "",
                momentum=item.factor_scores.get("momentum") or "",
                risk=item.factor_scores.get("risk_safety") or "",
                reasons=reasons,
                warnings=warnings,
            )
        )
    if report.hard_excluded:
        lines.append("")
        lines.append("## 硬性剔除")
        lines.append("")
        lines.append("| Ticker | Category | Severity | Field | Raw value | Normalized value | Threshold | 原因 |")
        lines.append("| --- | --- | --- | --- | ---: | ---: | ---: | --- |")
        for item in report.hard_excluded:
            detail = item.exclusion_details[0] if item.exclusion_details else {}
            lines.append(
                "| {ticker} | {category} | {severity} | {field} | {raw} | {normalized} | {threshold} | {reason} |".format(
                    ticker=item.ticker,
                    category=detail.get("category", ""),
                    severity=detail.get("severity", ""),
                    field=detail.get("field", ""),
                    raw=detail.get("raw_value", ""),
                    normalized=detail.get("normalized_value", ""),
                    threshold=detail.get("threshold", ""),
                    reason=item.excluded_reason,
                )
            )
    if report.soft_penalties:
        lines.append("")
        lines.append("## 扣分標記")
        lines.append("")
        lines.append("| Ticker | 扣分 | 原因 |")
        lines.append("| --- | ---: | --- |")
        for item in report.soft_penalties:
            reasons = "；".join(item.get("reasons", [])) or "有扣分"
            lines.append(f"| {item['ticker']} | {item.get('penalty_score')} | {reasons} |")
    if report.missing_data_warnings:
        lines.append("")
        lines.append("## 資料缺口提示")
        lines.append("")
        lines.append("| Ticker | 缺少欄位 | 動作限制 | 資料品質旗標 |")
        lines.append("| --- | --- | --- | --- |")
        for item in report.missing_data_warnings:
            missing_fields = "、".join(item.get("missing_fields", [])) or "部分欄位缺失"
            action_cap = item.get("action_cap_reason") or ""
            flags = "；".join(item.get("data_quality_flags", [])) or ""
            lines.append(f"| {item['ticker']} | {missing_fields} | {action_cap} | {flags} |")
    return "\n".join(lines)


def _render_json(report: ScreeningReport) -> str:
    payload = {
        "strategy_mode": report.strategy_mode,
        "review_mode": report.review_mode,
        "universe_size": report.universe_size,
        "hard_pass_count": report.hard_pass_count,
        "candidate_count": len(report.candidates),
        "min_score": report.min_score,
        "effective_min_score_source": report.effective_min_score_source,
        "top_n": report.top_n,
        "dedupe_company": report.dedupe_company,
        "hard_excluded_count": len(report.hard_excluded),
        "soft_penalty_count": len(report.soft_penalties),
        "missing_data_warning_count": len(report.missing_data_warnings),
        "retry_failed_count": report.retry_failed_count,
        "fetch_failed_count": report.fetch_failed_count,
        "dedupe_removed_count": report.dedupe_removed_count,
        "ranking_style": report.ranking_style,
        "top_n_average_total_score": report.top_n_average_total_score,
        "top_n_average_fundamental_score": report.top_n_average_fundamental_score,
        "top_n_average_momentum_score": report.top_n_average_momentum_score,
        "top_n_average_risk_safety_score": report.top_n_average_risk_safety_score,
        "high_risk_candidate_count": report.high_risk_candidate_count,
        "expensive_candidate_count": report.expensive_candidate_count,
        "high_volatility_candidate_count": report.high_volatility_candidate_count,
        "deep_drawdown_candidate_count": report.deep_drawdown_candidate_count,
        "missing_data_candidate_count": report.missing_data_candidate_count,
        "sector_metadata_coverage": report.sector_metadata_coverage,
        "industry_metadata_coverage": report.industry_metadata_coverage,
        "metadata_fetch_failed_count": report.metadata_fetch_failed_count,
        "metadata_missing_count": report.metadata_missing_count,
        "sector_aware_status": report.sector_aware_status,
        "sector_aware_official_scoring": report.sector_aware_official_scoring,
        "sector_aware_shadow_mode": report.sector_aware_shadow_mode,
        "sector_aware_preview_available_count": report.sector_aware_preview_available_count,
        "sector_aware_preview_missing_count": report.sector_aware_preview_missing_count,
        "sector_aware_average_score_delta": report.sector_aware_average_score_delta,
        "sector_aware_rank_changed_count": report.sector_aware_rank_changed_count,
        "sector_aware_top_movers_up": report.sector_aware_top_movers_up,
        "sector_aware_top_movers_down": report.sector_aware_top_movers_down,
        "sector_aware_preview_coverage": report.sector_aware_preview_coverage,
        "sector_aware_score_correlation_with_current": report.sector_aware_score_correlation_with_current,
        "sector_aware_top_10_overlap": report.sector_aware_top_10_overlap,
        "sector_aware_top_10_overlap_total": report.sector_aware_top_10_overlap_total,
        "sector_aware_industry_peer_used_count": report.sector_aware_industry_peer_used_count,
        "sector_aware_sector_only_peer_used_count": report.sector_aware_sector_only_peer_used_count,
        "sector_aware_sector_peer_used_count": report.sector_aware_sector_peer_used_count,
        "sector_aware_universe_fallback_count": report.sector_aware_universe_fallback_count,
        "sector_aware_missing_sector_count": report.sector_aware_missing_sector_count,
        "sector_aware_universe_missing_metadata_count": report.sector_aware_universe_missing_metadata_count,
        "sector_aware_not_scored_disabled_count": report.sector_aware_not_scored_disabled_count,
        "sector_aware_average_peer_count": report.sector_aware_average_peer_count,
        "sector_aware_min_peer_count": report.sector_aware_min_peer_count,
        "sector_aware_max_peer_count": report.sector_aware_max_peer_count,
        "sector_aware_large_rank_change_count": report.sector_aware_large_rank_change_count,
        "sector_aware_large_rank_change_threshold": report.sector_aware_large_rank_change_threshold,
        "sector_aware_largest_movers": report.sector_aware_largest_movers,
        "market_context": report.market_context,
        "configured_composite_weights": report.configured_composite_weights,
        "effective_composite_weights": report.effective_composite_weights,
        "market_regime": report.market_regime,
        "market_regime_status": report.market_regime_status,
        "market_regime_signals": report.market_regime_signals,
        "candidates": [
            {
                "ticker": item.ticker,
                "strategy_mode": item.strategy_mode,
                "total_score": item.total_score,
                "base_total_score": item.base_total_score,
                "market_regime_score_delta": item.market_regime_score_delta,
                "legacy_total_score": item.legacy_total_score,
                "legacy_raw_score": item.legacy_raw_score,
                "legacy_adjusted_score": item.legacy_adjusted_score,
                "legacy_fundamental_score": item.legacy_fundamental_score,
                "legacy_momentum_score": item.legacy_momentum_score,
                "legacy_risk_safety_score": item.legacy_risk_safety_score,
                "raw_score": item.raw_score,
                "penalty_score": item.penalty_score,
                "confidence_multiplier": item.confidence_multiplier,
                "final_score": item.final_score,
                "adjusted_score": item.adjusted_score,
                "fundamental_score": item.fundamental_score,
                "momentum_score": item.momentum_score,
                "risk_safety_score": item.risk_safety_score,
                "factor_scores": item.factor_scores,
                "reasons": item.reasons,
                "risk_warnings": item.risk_warnings,
                "confidence_notes": item.confidence_notes,
                "penalties": item.penalties,
                "confidence_score": item.confidence_score,
                "confidence_label": item.confidence_label,
                "data_quality_score": item.data_quality_score,
                "data_quality_flags": item.data_quality_flags,
                "normalization_notes": item.normalization_notes,
                "company_snapshot": item.company_snapshot,
                "suggested_action": item.suggested_action,
                "action_cap_reason": item.action_cap_reason,
                "hard_exclusion": item.hard_exclusion,
                "sector_relative_score_preview": item.sector_relative_score_preview,
                "sector_relative_rank_preview": item.sector_relative_rank_preview,
                "sector_relative_score_delta": item.sector_relative_score_delta,
                "sector_relative_rank_delta": item.sector_relative_rank_delta,
                "sector_relative_notes": item.sector_relative_notes,
                "sector_relative_factor_scores": item.sector_relative_factor_scores,
                "sector_relative_peer_source": item.sector_relative_peer_source,
                "sector_relative_peer_count": item.sector_relative_peer_count,
                "sector_relative_peer_reason": item.sector_relative_peer_reason,
                "official_score_source": item.official_score_source,
            }
            for item in report.candidates
        ],
        "hard_excluded": [
            {
                "ticker": item.ticker,
                "strategy_mode": item.strategy_mode,
                "excluded": True,
                "excluded_reason": item.excluded_reason,
                "exclusion_reasons": item.exclusion_reasons,
                "exclusion_details": item.exclusion_details,
                "risk_warnings": item.risk_warnings,
                "confidence_notes": item.confidence_notes,
                "data_quality_score": item.data_quality_score,
                "data_quality_flags": item.data_quality_flags,
                "normalization_notes": item.normalization_notes,
                "action_cap_reason": item.action_cap_reason,
            }
            for item in report.hard_excluded
        ],
        "deduped": [
            {
                "ticker": item.ticker,
                "strategy_mode": item.strategy_mode,
                "excluded_reason": item.excluded_reason,
                "exclusion_reasons": item.exclusion_reasons,
                "exclusion_details": item.exclusion_details,
                "kept": item.exclusion_details[0].get("threshold") if item.exclusion_details else None,
            }
            for item in report.deduped
        ],
        "soft_penalties": report.soft_penalties,
        "missing_data_warnings": report.missing_data_warnings,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def build_report(
    records: Iterable[Dict[str, Any]] | Iterable[StockRecord],
    config: Optional[ScreenConfig] = None,
    top_n: Optional[int] = None,
    *,
    strategy_mode: str = DEFAULT_STRATEGY_MODE,
    as_of: Optional[date] = None,
    force_rebalance: bool = False,
    min_score: Optional[float] = None,
    market_context: Optional[Dict[str, Any]] = None,
) -> ScreeningReport:
    canonical_records: List[StockRecord] = []
    for record in records:
        if isinstance(record, StockRecord):
            canonical_records.append(record)
        else:
            canonical_records.append(_canonicalize_record(record))
    return screen_records(
        canonical_records,
        config=config,
        top_n=top_n,
        strategy_mode=strategy_mode,
        as_of=as_of,
        force_rebalance=force_rebalance,
        min_score=min_score,
        market_context=market_context,
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rank a US stock universe with hybrid scoring.")
    parser.add_argument("--input", required=True, help="CSV, JSON, or JSONL file with one row per ticker")
    parser.add_argument("--market-context", help="Optional market context sidecar JSON generated by fetch_yfinance_snapshot.py")
    parser.add_argument("--config", help="Optional JSON config file")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--min-score", type=float, help="Optional score floor. Defaults to unset for both modes; use this only when you want a fixed cutoff.")
    parser.add_argument("--as-of", dest="as_of", help="Optional YYYY-MM-DD date")
    parser.add_argument("--strategy-mode", choices=sorted(VALID_STRATEGY_MODES), default=DEFAULT_STRATEGY_MODE)
    parser.add_argument("--force-rebalance", action="store_true")
    return parser.parse_args(argv)


def _load_config(path: Optional[str]) -> ScreenConfig:
    if not path:
        return ScreenConfig()
    payload = json.loads(Path(path).read_text())
    if not isinstance(payload, dict):
        raise ValueError("config must be a JSON object")
    return ScreenConfig.from_dict(payload)


def _load_as_of(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    return date.fromisoformat(value)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    records = load_records(Path(args.input))
    market_context = load_market_context(args.market_context) if args.market_context else None
    config = _load_config(args.config)
    as_of = _load_as_of(args.as_of)
    report = screen_records(
        records,
        config=config,
        top_n=args.top_n,
        strategy_mode=args.strategy_mode,
        as_of=as_of,
        force_rebalance=args.force_rebalance,
        min_score=args.min_score,
        market_context=market_context,
    )
    if args.format == "json":
        print(_render_json(report))
    else:
        print(_render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
