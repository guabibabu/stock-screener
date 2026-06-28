#!/usr/bin/env python3
"""Fetch and normalize a yfinance snapshot for the US stock screener."""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import pstdev
from typing import Any, Dict, Iterable, List, Optional, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
DEFAULT_WATCHLIST_PATH = SKILL_ROOT / "references" / "sample-watchlist.csv"
DEFAULT_SNAPSHOT_PATH = SKILL_ROOT / "data" / "latest_snapshot.json"
DEFAULT_MARKET_CONTEXT_PATH = SKILL_ROOT / "data" / "latest_snapshot.market-context.json"
DEFAULT_BATCH_SIZE = 25
DEFAULT_RETRY_ATTEMPTS = 2
RETRYABLE_ERROR_KEYWORDS = (
    "timeout",
    "timed out",
    "curl",
    "connection",
    "temporarily unavailable",
    "429",
    "rate limit",
    "remote end closed",
)
MARKET_CONTEXT_INDEX_TICKERS = ("SPY", "QQQ", "^VIX")


class YFinanceUnavailableError(RuntimeError):
    """Raised when the yfinance package is missing."""


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


def _safe_percent(value: Any) -> Optional[float]:
    number = _coerce_float(value)
    if number is None:
        return None
    if abs(number) <= 1.5:
        return number * 100.0
    return number


def _parse_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if not text:
        return None
    for separator in ("T", " "):
        if separator in text:
            text = text.split(separator, 1)[0]
            break
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _rows_from_history(history: Any) -> List[Dict[str, Any]]:
    if history is None:
        return []
    if isinstance(history, list):
        return [row for row in history if isinstance(row, dict)]
    if isinstance(history, dict):
        return [history]
    if hasattr(history, "reset_index") and hasattr(history, "to_dict"):
        try:
            return history.reset_index().to_dict("records")
        except Exception:
            pass
    if hasattr(history, "to_dict"):
        try:
            return history.to_dict("records")
        except Exception:
            pass
    return []


def _financial_statement_value(statement: Any, *labels: str) -> Optional[float]:
    if statement is None:
        return None
    lowered_labels = {label.lower().replace(" ", "").replace("_", "") for label in labels}
    if isinstance(statement, dict):
        for key, value in statement.items():
            normalized_key = str(key).lower().replace(" ", "").replace("_", "")
            if normalized_key in lowered_labels:
                if isinstance(value, dict):
                    for nested_value in value.values():
                        number = _coerce_float(nested_value)
                        if number is not None:
                            return number
                if isinstance(value, list):
                    for nested_value in value:
                        number = _coerce_float(nested_value)
                        if number is not None:
                            return number
                number = _coerce_float(value)
                if number is not None:
                    return number
        return None
    if hasattr(statement, "index") and hasattr(statement, "loc"):
        try:
            index_lookup = {
                str(index).lower().replace(" ", "").replace("_", ""): index
                for index in list(statement.index)
            }
            for normalized_label in lowered_labels:
                if normalized_label not in index_lookup:
                    continue
                row = statement.loc[index_lookup[normalized_label]]
                values = row.tolist() if hasattr(row, "tolist") else list(row)
                for value in values:
                    number = _coerce_float(value)
                    if number is not None:
                        return number
        except Exception:
            return None
    return None


def _free_cash_flow_from_cashflow(cashflow: Any) -> Optional[float]:
    operating_cash_flow = _financial_statement_value(
        cashflow,
        "Operating Cash Flow",
        "Total Cash From Operating Activities",
        "operating_cash_flow",
        "operatingCashFlow",
    )
    capital_expenditure_raw = _financial_statement_value(
        cashflow,
        "Capital Expenditure",
        "Capital Expenditures",
        "capital_expenditure",
        "capitalExpenditure",
    )
    if operating_cash_flow is None or capital_expenditure_raw is None:
        return None
    capital_expenditure = abs(capital_expenditure_raw)
    return operating_cash_flow - capital_expenditure


def _share_history_points(shares: Any) -> List[tuple[date, float]]:
    points: List[tuple[date, float]] = []
    if shares is None:
        return points
    if isinstance(shares, dict):
        iterator = shares.items()
    elif hasattr(shares, "items"):
        try:
            iterator = shares.items()
        except Exception:
            iterator = []
    elif isinstance(shares, list):
        iterator = []
        for row in shares:
            if isinstance(row, dict):
                row_date = _parse_date(_pick(row, "Date", "date", "index"))
                value = _coerce_float(_pick(row, "Shares", "shares", "sharesOutstanding", "value"))
                if row_date is not None and value is not None:
                    points.append((row_date, value))
        return points
    else:
        iterator = []
    for raw_date, raw_value in iterator:
        row_date = _parse_date(raw_date)
        value = _coerce_float(raw_value)
        if row_date is not None and value is not None and value > 0:
            points.append((row_date, value))
    points.sort(key=lambda item: item[0])
    return points


def _shares_growth_yoy_from_history(shares: Any) -> Optional[float]:
    points = _share_history_points(shares)
    if len(points) < 2:
        return None
    latest_date, latest_value = points[-1]
    target_date = latest_date - timedelta(days=365)
    base_candidates = [point for point in points[:-1] if point[0] <= target_date]
    base_date, base_value = base_candidates[-1] if base_candidates else points[0]
    if base_value <= 0 or base_date == latest_date:
        return None
    return ((latest_value / base_value) - 1.0) * 100.0


def _history_value(row: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] not in ("", None):
            return row[key]
    return None


def _close_from_row(row: Dict[str, Any]) -> Optional[float]:
    return _coerce_float(_history_value(row, "Adj Close", "Close", "close", "adj_close"))


def _volume_from_row(row: Dict[str, Any]) -> Optional[float]:
    return _coerce_float(_history_value(row, "Volume", "volume"))


def _date_from_row(row: Dict[str, Any]) -> Optional[date]:
    return _parse_date(_history_value(row, "Date", "Datetime", "date", "datetime", "index"))


def _moving_average(values: Sequence[float], window: int) -> Optional[float]:
    if len(values) < window or window <= 0:
        return None
    subset = values[-window:]
    return sum(subset) / float(window)


def _returns(values: Sequence[float]) -> List[float]:
    returns: List[float] = []
    for previous, current in zip(values, values[1:]):
        if previous <= 0:
            continue
        returns.append((current / previous) - 1.0)
    return returns


def _annualized_volatility(values: Sequence[float]) -> Optional[float]:
    returns = _returns(values)
    if len(returns) < 2:
        return None
    return pstdev(returns) * math.sqrt(252.0) * 100.0


def _max_drawdown(values: Sequence[float]) -> Optional[float]:
    if len(values) < 2:
        return None
    peak = values[0]
    max_drawdown = 0.0
    for price in values[1:]:
        if price > peak:
            peak = price
        if peak > 0:
            drawdown = (peak - price) / peak * 100.0
            max_drawdown = max(max_drawdown, drawdown)
    return max_drawdown


def _coerce_price(info: Dict[str, Any], closes: Sequence[float]) -> Optional[float]:
    price = _coerce_float(_pick(info, "regularMarketPrice", "currentPrice", "lastPrice"))
    if price is not None:
        return price
    if closes:
        return closes[-1]
    return _coerce_float(_pick(info, "previousClose", "regularMarketPreviousClose"))


def _shares_outstanding(info: Dict[str, Any]) -> Optional[float]:
    return _coerce_float(_pick(info, "sharesOutstanding", "impliedSharesOutstanding"))


def _build_record(
    ticker: str,
    info: Dict[str, Any],
    history_rows: List[Dict[str, Any]],
    *,
    cashflow: Any = None,
    shares: Any = None,
    as_of: Optional[date] = None,
) -> Dict[str, Any]:
    as_of = as_of or date.today()
    closes: List[float] = []
    paired_rows: List[tuple[float, float]] = []
    row_dates: List[date] = []
    for row in history_rows:
        close = _close_from_row(row)
        volume = _volume_from_row(row)
        row_date = _date_from_row(row)
        if close is not None and close > 0:
            closes.append(close)
            if row_date is not None:
                row_dates.append(row_date)
            if volume is not None and volume >= 0:
                paired_rows.append((close, volume))

    price = _coerce_price(info, closes)
    avg_volume_20d = None
    if paired_rows:
        recent_volume_pairs = paired_rows[-20:]
        avg_volume_20d = sum(volume for _, volume in recent_volume_pairs) / len(recent_volume_pairs)
    avg_dollar_volume_20d = None
    if paired_rows:
        recent_dollar_pairs = [close * volume for close, volume in paired_rows[-20:] if close > 0 and volume >= 0]
        if recent_dollar_pairs:
            avg_dollar_volume_20d = sum(recent_dollar_pairs) / len(recent_dollar_pairs)

    latest_close = closes[-1] if closes else None
    sma50 = _moving_average(closes, 50)
    sma200 = _moving_average(closes, 200)
    price_vs_sma50_pct = ((price / sma50) - 1.0) * 100.0 if price is not None and sma50 not in (None, 0) else None
    price_vs_sma200_pct = ((price / sma200) - 1.0) * 100.0 if price is not None and sma200 not in (None, 0) else None
    relative_strength_252d = None
    if len(closes) >= 2 and closes[0] > 0:
        relative_strength_252d = ((closes[-1] / closes[0]) - 1.0) * 100.0
    volatility_63d = _annualized_volatility(closes[-64:] if len(closes) >= 3 else closes)
    max_drawdown_252d = _max_drawdown(closes[-252:] if len(closes) >= 2 else closes)
    latest_history_date = row_dates[-1] if row_dates else None
    data_age_days = (as_of - latest_history_date).days if latest_history_date is not None else 0

    market_cap = _coerce_float(_pick(info, "marketCap"))
    if market_cap is None and price is not None:
        shares_outstanding = _shares_outstanding(info)
        if shares_outstanding is not None:
            market_cap = price * shares_outstanding

    quote_type = str(_pick(info, "quoteType", "type") or "").strip().upper()
    exchange = _pick(info, "fullExchangeName", "exchange")
    exchange_text = str(exchange or "").strip()
    sector = _clean_metadata_text(_pick(info, "sector"))
    industry = _clean_metadata_text(_pick(info, "industry"))
    metadata_fetch_failed = bool(info.get("__metadata_fetch_failed__"))
    metadata_missing = not metadata_fetch_failed and (sector is None or industry is None)
    is_otc = "OTC" in exchange_text.upper()
    is_etf = quote_type == "ETF"
    is_adr = "ADR" in quote_type
    halted = str(_pick(info, "tradingStatus", "marketState") or "").upper() == "HALTED"

    notes: List[str] = []
    if not history_rows:
        notes.append("未取得歷史價格，僅能使用公開參考欄位")
    if price is None:
        notes.append("未取得最新價格")
    if market_cap is None:
        notes.append("未取得市值")
    if avg_dollar_volume_20d is None:
        notes.append("未取得 20 日均成交額")

    debt_to_equity_raw = _coerce_float(_pick(info, "debtToEquity"))
    debt_to_equity_normalized = None if debt_to_equity_raw is None else (debt_to_equity_raw / 100.0 if debt_to_equity_raw > 10 else debt_to_equity_raw)
    free_cash_flow = _coerce_float(_pick(info, "freeCashflow", "freeCashFlow"))
    if free_cash_flow is None:
        free_cash_flow = _free_cash_flow_from_cashflow(cashflow)
    shares_growth_yoy = _shares_growth_yoy_from_history(shares)
    record = {
        "ticker": _normalize_ticker(ticker),
        "sector": sector,
        "industry": industry,
        "price": price,
        "market_cap": market_cap,
        "avg_dollar_volume_20d": avg_dollar_volume_20d,
        "avg_volume_20d": avg_volume_20d,
        "revenue_growth_yoy": _safe_percent(_pick(info, "revenueGrowth")),
        "eps_growth_yoy": _safe_percent(_pick(info, "earningsGrowth")),
        "gross_margin": _safe_percent(_pick(info, "grossMargins")),
        "operating_margin": _safe_percent(_pick(info, "operatingMargins")),
        "return_on_equity": _safe_percent(_pick(info, "returnOnEquity")),
        "free_cash_flow": free_cash_flow,
        "roic": None,
        "shares_growth_yoy": shares_growth_yoy,
        "pe_ratio": _coerce_float(_pick(info, "trailingPE", "forwardPE")),
        "ps_ratio": _coerce_float(_pick(info, "priceToSalesTrailing12Months", "priceToBook")),
        "relative_strength_252d": relative_strength_252d,
        "price_vs_sma50_pct": price_vs_sma50_pct,
        "price_vs_sma200_pct": price_vs_sma200_pct,
        "beta": _coerce_float(_pick(info, "beta")),
        "volatility_63d": volatility_63d,
        "max_drawdown_252d": max_drawdown_252d,
        "debt_to_equity_raw": debt_to_equity_raw,
        "debt_to_equity_normalized": debt_to_equity_normalized,
        "debt_to_equity": debt_to_equity_normalized,
        "data_age_days": data_age_days,
        "price_data_age_days": data_age_days,
        "fundamental_data_age_days": None,
        "shares_data_age_days": None,
        "market_cap_timestamp": latest_history_date.isoformat() if latest_history_date is not None else None,
        "halted": halted,
        "is_otc": is_otc,
        "is_etf": is_etf,
        "is_adr": is_adr,
        "security_type": "common_stock" if quote_type in {"EQUITY", "COMMON STOCK", "STOCK"} else (quote_type.lower() or None),
        "exchange": exchange_text or None,
        "notes": "; ".join(notes) if notes else None,
        "raw": {
            "source": "yfinance",
            "history_points": len(history_rows),
            "latest_history_date": latest_history_date.isoformat() if latest_history_date is not None else None,
            "latest_close": latest_close,
            "metadata_fetch_failed": metadata_fetch_failed,
            "metadata_missing": metadata_missing,
        },
    }
    return record


def _history_lookup(history_rows: List[Dict[str, Any]]) -> Dict[date, float]:
    lookup: Dict[date, float] = {}
    for row in history_rows:
        row_date = _date_from_row(row)
        close = _close_from_row(row)
        if row_date is not None and close is not None and close > 0:
            lookup[row_date] = close
    return lookup


def _sma_up_to_date(history_rows: List[Dict[str, Any]], target_date: date, window: int) -> Optional[float]:
    closes: List[float] = []
    for row in history_rows:
        row_date = _date_from_row(row)
        close = _close_from_row(row)
        if row_date is None or close is None or close <= 0:
            continue
        if row_date <= target_date:
            closes.append(close)
    return _moving_average(closes, window)


def _derive_market_context_path(snapshot_path: Path | str) -> Path:
    path = Path(snapshot_path)
    suffix = path.suffix or ".json"
    if suffix == ".json":
        return path.with_name(f"{path.stem}.market-context.json")
    return path.with_name(f"{path.name}.market-context.json")


def build_market_context(
    records: Sequence[Dict[str, Any]],
    *,
    provider: Optional[Any] = None,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
) -> Dict[str, Any]:
    provider = provider or YFinanceProvider()
    index_payloads: Dict[str, Dict[str, Any]] = {}
    for ticker in MARKET_CONTEXT_INDEX_TICKERS:
        try:
            payload, _retries_used, _retry_failed = _fetch_with_retries(provider, ticker, retry_attempts)
        except Exception:
            payload = {}
        index_payloads[ticker] = payload if isinstance(payload, dict) else {}

    index_histories = {
        ticker: _rows_from_history(payload.get("history"))
        for ticker, payload in index_payloads.items()
    }
    date_sets = []
    for ticker in MARKET_CONTEXT_INDEX_TICKERS:
        lookup = _history_lookup(index_histories.get(ticker, []))
        if lookup:
            date_sets.append(set(lookup.keys()))
    common_dates = set.intersection(*date_sets) if len(date_sets) == len(MARKET_CONTEXT_INDEX_TICKERS) else set()
    latest_common_date = max(common_dates) if common_dates else None

    market_context = {
        "as_of_date": latest_common_date.isoformat() if latest_common_date is not None else None,
        "spy_close": None,
        "spy_sma200": None,
        "qqq_close": None,
        "qqq_sma200": None,
        "vix_close": None,
        "breadth_above_200dma": None,
        "breadth_eligible_count": 0,
        "market_context_source": "yfinance_sidecar",
    }
    if latest_common_date is not None:
        spy_lookup = _history_lookup(index_histories.get("SPY", []))
        qqq_lookup = _history_lookup(index_histories.get("QQQ", []))
        vix_lookup = _history_lookup(index_histories.get("^VIX", []))
        market_context["spy_close"] = spy_lookup.get(latest_common_date)
        market_context["qqq_close"] = qqq_lookup.get(latest_common_date)
        market_context["vix_close"] = vix_lookup.get(latest_common_date)
        market_context["spy_sma200"] = _sma_up_to_date(index_histories.get("SPY", []), latest_common_date, 200)
        market_context["qqq_sma200"] = _sma_up_to_date(index_histories.get("QQQ", []), latest_common_date, 200)

    breadth_eligible = 0
    breadth_above = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        value = _coerce_float(_pick(record, "price_vs_sma200_pct", "price_vs_200dma"))
        if value is None:
            continue
        breadth_eligible += 1
        if value >= 0:
            breadth_above += 1
    market_context["breadth_eligible_count"] = breadth_eligible
    if breadth_eligible > 0:
        market_context["breadth_above_200dma"] = round(breadth_above / breadth_eligible, 3)
    signals = {
        "spy": "missing",
        "qqq": "missing",
        "vix": "missing",
        "breadth": "missing",
    }
    spy_close = _coerce_float(market_context.get("spy_close"))
    spy_sma200 = _coerce_float(market_context.get("spy_sma200"))
    if spy_close is not None and spy_sma200 not in (None, 0):
        signals["spy"] = "risk_on" if spy_close > spy_sma200 else "risk_off"
    qqq_close = _coerce_float(market_context.get("qqq_close"))
    qqq_sma200 = _coerce_float(market_context.get("qqq_sma200"))
    if qqq_close is not None and qqq_sma200 not in (None, 0):
        signals["qqq"] = "risk_on" if qqq_close > qqq_sma200 else "risk_off"
    vix_close = _coerce_float(market_context.get("vix_close"))
    if vix_close is not None:
        if vix_close < 20:
            signals["vix"] = "risk_on"
        elif vix_close >= 25:
            signals["vix"] = "risk_off"
        else:
            signals["vix"] = "neutral"
    breadth = _coerce_float(market_context.get("breadth_above_200dma"))
    if breadth is not None:
        if breadth >= 0.60:
            signals["breadth"] = "risk_on"
        elif breadth <= 0.40:
            signals["breadth"] = "risk_off"
        else:
            signals["breadth"] = "neutral"
    market_context["signals"] = signals
    return market_context


def save_market_context(context: Dict[str, Any], path: Path | str) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def _failed_record(ticker: str, error: str, *, as_of: Optional[date] = None) -> Dict[str, Any]:
    as_of = as_of or date.today()
    return {
        "ticker": _normalize_ticker(ticker),
        "data_age_days": 0,
        "price_data_age_days": None,
        "fundamental_data_age_days": None,
        "shares_data_age_days": None,
        "notes": f"抓取失敗：{error}",
        "raw": {
            "source": "yfinance",
            "error": error,
            "fetched_at": as_of.isoformat(),
            "fetch_status": "failed",
            "fetch_failed": True,
            "retry_failed": True,
            "metadata_fetch_failed": True,
            "metadata_missing": False,
        },
    }


def _read_json_payload(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path.name}: {exc.msg}") from exc


def load_watchlist(path: Path | str) -> List[str]:
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(file_path)
    suffix = file_path.suffix.lower()
    tickers: List[str] = []

    if suffix == ".csv":
        with file_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames:
                for row in reader:
                    ticker = _pick(row, "ticker", "symbol", "code")
                    if ticker:
                        tickers.append(_normalize_ticker(ticker))
            else:
                handle.seek(0)
                csv_reader = csv.reader(handle)
                for row in csv_reader:
                    if row and row[0].strip():
                        tickers.append(_normalize_ticker(row[0]))
        return _dedupe_tickers(tickers)

    if suffix in {".json", ".jsonl", ".ndjson"}:
        if suffix == ".json":
            payload = _read_json_payload(file_path)
            rows = payload.get("records") if isinstance(payload, dict) and "records" in payload else payload
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict):
                        ticker = _pick(row, "ticker", "symbol", "code")
                        if ticker:
                            tickers.append(_normalize_ticker(ticker))
                    elif row:
                        tickers.append(_normalize_ticker(row))
            return _dedupe_tickers(tickers)

        for line in file_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if isinstance(row, dict):
                ticker = _pick(row, "ticker", "symbol", "code")
                if ticker:
                    tickers.append(_normalize_ticker(ticker))
            else:
                tickers.append(_normalize_ticker(row))
        return _dedupe_tickers(tickers)

    raise ValueError("Unsupported watchlist format. Use CSV, JSON, JSONL, or NDJSON.")


def _dedupe_tickers(tickers: Iterable[str]) -> List[str]:
    seen = set()
    ordered: List[str] = []
    for ticker in tickers:
        normalized = _normalize_ticker(ticker)
        if normalized and normalized not in seen:
            seen.add(normalized)
            ordered.append(normalized)
    return ordered


def _import_yfinance():
    try:
        import yfinance as yf  # type: ignore
    except ImportError as exc:
        raise YFinanceUnavailableError(
            "yfinance is not installed. Run: python3 -m pip install yfinance"
        ) from exc
    return yf


class YFinanceProvider:
    """Thin adapter around the yfinance package."""

    def __init__(self) -> None:
        self._yf = _import_yfinance()

    def fetch(self, ticker: str) -> Dict[str, Any]:
        symbol = self._yf.Ticker(_normalize_ticker(ticker))
        info: Dict[str, Any] = {}
        history = []
        cashflow = None
        shares = None
        info_error = None
        history_error = None
        cashflow_error = None
        shares_error = None
        try:
            raw_info = symbol.info
            if isinstance(raw_info, dict):
                info = raw_info
        except Exception as exc:  # pragma: no cover - depends on live Yahoo
            info_error = str(exc)
            info["__metadata_fetch_failed__"] = True
        try:
            history_frame = symbol.history(period="1y", interval="1d", auto_adjust=False, actions=False)
            history = _rows_from_history(history_frame)
        except Exception as exc:  # pragma: no cover - depends on live Yahoo
            history_error = str(exc)
        if _pick(info, "freeCashflow", "freeCashFlow") is None:
            try:
                cashflow = getattr(symbol, "cashflow", None)
            except Exception as exc:  # pragma: no cover - depends on live Yahoo
                cashflow_error = str(exc)
        try:
            start = (date.today() - timedelta(days=550)).isoformat()
            shares = symbol.get_shares_full(start=start)
        except Exception as exc:  # pragma: no cover - depends on live Yahoo
            shares_error = str(exc)
        return {
            "info": info,
            "history": history,
            "cashflow": cashflow,
            "shares": shares,
            "errors": [error for error in (info_error, history_error, cashflow_error, shares_error) if error],
        }


def _is_retryable_error(message: Any) -> bool:
    text = str(message or "").lower()
    return any(keyword in text for keyword in RETRYABLE_ERROR_KEYWORDS)


def _payload_errors(payload: Any) -> List[str]:
    if not isinstance(payload, dict):
        return []
    errors = payload.get("errors") or []
    if isinstance(errors, list):
        return [str(error) for error in errors if error]
    if errors:
        return [str(errors)]
    return []


def _payload_has_market_data(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    info = payload.get("info")
    history = payload.get("history")
    if isinstance(info, dict) and any(info.get(key) is not None for key in ("regularMarketPrice", "currentPrice", "marketCap")):
        return True
    try:
        return bool(history)
    except Exception:
        return False


def _fetch_with_retries(provider: Any, ticker: str, retry_attempts: int) -> tuple[Dict[str, Any], int, bool]:
    last_payload: Dict[str, Any] = {}
    last_error: Optional[Exception] = None
    retries_used = 0
    for attempt in range(retry_attempts + 1):
        try:
            payload = provider.fetch(ticker)
            if isinstance(payload, dict):
                last_payload = payload
            else:
                last_payload = {}
            retryable_payload_errors = [error for error in _payload_errors(last_payload) if _is_retryable_error(error)]
            if retryable_payload_errors and not _payload_has_market_data(last_payload) and attempt < retry_attempts:
                retries_used += 1
                continue
            retry_failed = bool(retryable_payload_errors and not _payload_has_market_data(last_payload) and attempt >= retry_attempts)
            return last_payload, retries_used, retry_failed
        except Exception as exc:  # pragma: no cover - depends on live Yahoo
            last_error = exc
            if _is_retryable_error(exc) and attempt < retry_attempts:
                retries_used += 1
                continue
            raise
    if last_error is not None:
        raise last_error
    return last_payload, retries_used, retry_attempts > 0


def _chunked(values: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    size = max(1, int(size))
    for index in range(0, len(values), size):
        yield values[index : index + size]


@dataclass
class SnapshotBundle:
    source_name: str
    as_of: str
    requested_as_of: str
    fetched_at: str
    source_data_end_date: Optional[str] = None
    point_in_time_verified: bool = False
    status: str = "ok"
    records: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    universe: List[str] = field(default_factory=list)
    retry_failed_count: int = 0
    fetch_failed_count: int = 0
    sector_metadata_coverage: float = 0.0
    industry_metadata_coverage: float = 0.0
    metadata_fetch_failed_count: int = 0
    metadata_missing_count: int = 0
    batch_size: int = DEFAULT_BATCH_SIZE
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS

    def to_payload(self) -> Dict[str, Any]:
        return {
            "metadata": {
                "source_name": self.source_name,
                "as_of": self.as_of,
                "requested_as_of": self.requested_as_of,
                "fetched_at": self.fetched_at,
                "source_data_end_date": self.source_data_end_date,
                "point_in_time_verified": self.point_in_time_verified,
                "status": self.status,
                "universe_size": len(self.universe),
                "record_count": len(self.records),
                "retry_failed_count": self.retry_failed_count,
                "fetch_failed_count": self.fetch_failed_count,
                "sector_metadata_coverage": self.sector_metadata_coverage,
                "industry_metadata_coverage": self.industry_metadata_coverage,
                "metadata_fetch_failed_count": self.metadata_fetch_failed_count,
                "metadata_missing_count": self.metadata_missing_count,
                "batch_size": self.batch_size,
                "retry_attempts": self.retry_attempts,
            },
            "universe": self.universe,
            "records": self.records,
            "warnings": self.warnings,
            "errors": self.errors,
        }


def fetch_snapshot(
    tickers: Sequence[str],
    *,
    provider: Optional[Any] = None,
    as_of: Optional[date] = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    retry_attempts: int = DEFAULT_RETRY_ATTEMPTS,
) -> SnapshotBundle:
    provider = provider or YFinanceProvider()
    as_of = as_of or date.today()
    normalized_tickers = _dedupe_tickers(tickers)
    records: List[Dict[str, Any]] = []
    warnings: List[str] = []
    errors: List[str] = []
    retry_failed_count = 0
    source_data_end_dates: List[date] = []
    for batch in _chunked(normalized_tickers, batch_size):
        for ticker in batch:
            try:
                payload, retries_used, retry_failed = _fetch_with_retries(provider, ticker, retry_attempts)
                if retry_failed:
                    retry_failed_count += 1
                info = payload.get("info") if isinstance(payload, dict) else {}
                history = payload.get("history") if isinstance(payload, dict) else []
                cashflow = payload.get("cashflow") if isinstance(payload, dict) else None
                shares = payload.get("shares") if isinstance(payload, dict) else None
                if isinstance(info, dict) and any("info" in error.lower() for error in _payload_errors(payload)):
                    info.setdefault("__metadata_fetch_failed__", True)
                record = _build_record(ticker, info or {}, _rows_from_history(history), cashflow=cashflow, shares=shares, as_of=as_of)
                fetch_errors = _payload_errors(payload)
                if fetch_errors:
                    warnings.extend(f"{ticker}: {message}" for message in fetch_errors)
                raw = record.setdefault("raw", {})
                raw["retry_count"] = retries_used
                raw["retry_failed"] = retry_failed
                raw["fetch_errors"] = fetch_errors
                if not _payload_has_market_data(payload):
                    raw["fetch_status"] = "failed"
                    raw["fetch_failed"] = True
                    record["notes"] = (record.get("notes") + "; " if record.get("notes") else "") + "抓取失敗造成缺資料"
                else:
                    raw["fetch_status"] = "ok" if not fetch_errors else "partial"
                    raw["fetch_failed"] = False
                if record.get("notes"):
                    warnings.append(f"{ticker}: {record['notes']}")
                latest_history_text = (record.get("raw") or {}).get("latest_history_date")
                latest_history_date = _parse_date(latest_history_text)
                if latest_history_date is not None:
                    source_data_end_dates.append(latest_history_date)
                records.append(record)
            except Exception as exc:
                message = f"{ticker}: {exc}"
                errors.append(message)
                warnings.append(message)
                records.append(_failed_record(ticker, str(exc), as_of=as_of))
                retry_failed_count += 1
    success_count = sum(
        1
        for record in records
        if any(
            record.get(field) is not None
            for field in ("price", "market_cap", "avg_dollar_volume_20d", "avg_volume_20d")
        )
    )
    if success_count == 0:
        status = "failed"
        warnings.insert(0, "yfinance 無法連線到 Yahoo Finance，這次沒有抓到可用市場資料")
    elif success_count < len(records):
        status = "partial"
        warnings.insert(0, f"只有 {success_count}/{len(records)} 檔成功抓到資料")
    else:
        status = "ok"
    fetch_failed_count = sum(1 for record in records if (record.get("raw") or {}).get("fetch_failed") is True)
    record_count = len(records)
    sector_metadata_coverage = (
        sum(1 for record in records if _clean_metadata_text(record.get("sector")) is not None) / record_count
        if record_count
        else 0.0
    )
    industry_metadata_coverage = (
        sum(1 for record in records if _clean_metadata_text(record.get("industry")) is not None) / record_count
        if record_count
        else 0.0
    )
    metadata_fetch_failed_count = sum(1 for record in records if (record.get("raw") or {}).get("metadata_fetch_failed") is True)
    metadata_missing_count = sum(1 for record in records if (record.get("raw") or {}).get("metadata_missing") is True)
    return SnapshotBundle(
        source_name="yfinance",
        as_of=as_of.isoformat(),
        requested_as_of=as_of.isoformat(),
        fetched_at=datetime.now().isoformat(timespec="seconds"),
        source_data_end_date=(max(source_data_end_dates).isoformat() if source_data_end_dates else None),
        point_in_time_verified=False,
        status=status,
        records=records,
        warnings=warnings,
        errors=errors,
        universe=normalized_tickers,
        retry_failed_count=retry_failed_count,
        fetch_failed_count=fetch_failed_count,
        sector_metadata_coverage=round(sector_metadata_coverage, 3),
        industry_metadata_coverage=round(industry_metadata_coverage, 3),
        metadata_fetch_failed_count=metadata_fetch_failed_count,
        metadata_missing_count=metadata_missing_count,
        batch_size=batch_size,
        retry_attempts=retry_attempts,
    )


def save_snapshot(bundle: SnapshotBundle, path: Path | str = DEFAULT_SNAPSHOT_PATH) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(bundle.to_payload(), ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def load_snapshot(path: Path | str) -> SnapshotBundle:
    file_path = Path(path)
    payload = _read_json_payload(file_path)
    if isinstance(payload, list):
        records = payload
        metadata = {}
        warnings: List[str] = []
        errors: List[str] = []
        universe: List[str] = []
    elif isinstance(payload, dict):
        records = payload.get("records", [])
        metadata = payload.get("metadata", {})
        warnings = list(payload.get("warnings", []) or [])
        errors = list(payload.get("errors", []) or [])
        universe = list(payload.get("universe", []) or [])
    else:
        raise ValueError("Snapshot file must contain a list or an object with records")

    if not isinstance(records, list):
        raise ValueError("Snapshot records must be a list")

    as_of = str(metadata.get("as_of") or metadata.get("date") or date.today().isoformat())
    requested_as_of = str(metadata.get("requested_as_of") or as_of)
    fetched_at = str(metadata.get("fetched_at") or metadata.get("created_at") or datetime.now().isoformat(timespec="seconds"))
    source_name = str(metadata.get("source_name") or metadata.get("source") or "yfinance")
    status = str(metadata.get("status") or "ok")
    return SnapshotBundle(
        source_name=source_name,
        as_of=as_of,
        requested_as_of=requested_as_of,
        fetched_at=fetched_at,
        source_data_end_date=(str(metadata.get("source_data_end_date")) if metadata.get("source_data_end_date") not in (None, "") else None),
        point_in_time_verified=bool(metadata.get("point_in_time_verified") is True),
        status=status,
        records=records,
        warnings=warnings,
        errors=errors,
        universe=universe or [_normalize_ticker(row.get("ticker")) for row in records if isinstance(row, dict) and row.get("ticker")],
        retry_failed_count=int(metadata.get("retry_failed_count") or 0),
        fetch_failed_count=int(metadata.get("fetch_failed_count") or 0),
        sector_metadata_coverage=float(metadata.get("sector_metadata_coverage") or 0.0),
        industry_metadata_coverage=float(metadata.get("industry_metadata_coverage") or 0.0),
        metadata_fetch_failed_count=int(metadata.get("metadata_fetch_failed_count") or 0),
        metadata_missing_count=int(metadata.get("metadata_missing_count") or 0),
        batch_size=int(metadata.get("batch_size") or DEFAULT_BATCH_SIZE),
        retry_attempts=int(metadata.get("retry_attempts") or DEFAULT_RETRY_ATTEMPTS),
    )


def load_market_context(path: Path | str) -> Dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Market context sidecar must contain a JSON object")
    return payload


def build_summary(bundle: SnapshotBundle) -> str:
    lines = [
        f"來源：{bundle.source_name}",
        f"快照日期：{bundle.as_of}",
        f"requested_as_of：{bundle.requested_as_of}",
        f"抓取時間：{bundle.fetched_at}",
        f"source_data_end_date：{bundle.source_data_end_date or 'unknown'}",
        f"point_in_time_verified：{str(bundle.point_in_time_verified).lower()}",
        f"股票數量：{len(bundle.records)}",
        f"retry_failed_count：{bundle.retry_failed_count}",
        f"fetch_failed_count：{bundle.fetch_failed_count}",
        f"sector_metadata_coverage：{bundle.sector_metadata_coverage}",
        f"industry_metadata_coverage：{bundle.industry_metadata_coverage}",
        f"metadata_fetch_failed_count：{bundle.metadata_fetch_failed_count}",
        f"metadata_missing_count：{bundle.metadata_missing_count}",
    ]
    if bundle.warnings:
        lines.append("")
        lines.append("提醒：")
        for warning in bundle.warnings[:10]:
            lines.append(f"- {warning}")
    return "\n".join(lines)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch a yfinance snapshot for the US stock screener.")
    parser.add_argument("--watchlist", help="CSV/JSON/JSONL file with tickers to fetch")
    parser.add_argument("--tickers", help="Comma-separated list of tickers")
    parser.add_argument("--output", help="Snapshot output path", default=str(DEFAULT_SNAPSHOT_PATH))
    parser.add_argument("--as-of", help="Requested snapshot date (YYYY-MM-DD)")
    parser.add_argument("--screen", action="store_true", help="Run the screener after fetching")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--retry-attempts", type=int, default=DEFAULT_RETRY_ATTEMPTS)
    return parser.parse_args(argv)


def _resolve_tickers(args: argparse.Namespace) -> List[str]:
    if args.tickers:
        return _dedupe_tickers(item for item in args.tickers.split(",") if item.strip())
    if args.watchlist:
        return load_watchlist(Path(args.watchlist))
    if DEFAULT_WATCHLIST_PATH.exists():
        return load_watchlist(DEFAULT_WATCHLIST_PATH)
    return ["AAPL", "MSFT", "NVDA"]


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    tickers = _resolve_tickers(args)
    requested_as_of = date.fromisoformat(args.as_of) if args.as_of else None
    bundle = fetch_snapshot(tickers, as_of=requested_as_of, batch_size=args.batch_size, retry_attempts=args.retry_attempts)
    output_path = save_snapshot(bundle, args.output)
    market_context = build_market_context(bundle.records, retry_attempts=args.retry_attempts)
    market_context_path = save_market_context(market_context, _derive_market_context_path(args.output))
    print(f"已儲存快照：{output_path}")
    print(f"已儲存市場 context：{market_context_path}")
    print(build_summary(bundle))

    if args.screen:
        from us_stock_screener import build_report

        report = build_report(bundle.records, market_context=market_context)
        if args.format == "json":
            print(json.dumps(report_to_payload(report), ensure_ascii=False, indent=2))
        else:
            print(report_to_markdown(report, bundle))
    return 0


def report_to_payload(report: Any) -> Dict[str, Any]:
    return {
        "universe_size": report.universe_size,
        "candidate_count": len(report.candidates),
        "excluded_count": len(report.excluded),
        "min_score": report.min_score,
        "effective_min_score_source": report.effective_min_score_source,
        "retry_failed_count": report.retry_failed_count,
        "fetch_failed_count": report.fetch_failed_count,
        "sector_metadata_coverage": report.sector_metadata_coverage,
        "industry_metadata_coverage": report.industry_metadata_coverage,
        "metadata_fetch_failed_count": report.metadata_fetch_failed_count,
        "metadata_missing_count": report.metadata_missing_count,
        "sector_aware_status": report.sector_aware_status,
        "market_context": report.market_context,
        "configured_composite_weights": report.configured_composite_weights,
        "effective_composite_weights": report.effective_composite_weights,
        "market_regime": report.market_regime,
        "market_regime_status": report.market_regime_status,
        "market_regime_signals": report.market_regime_signals,
        "dedupe_removed_count": report.dedupe_removed_count,
        "candidates": [
            {
                "ticker": item.ticker,
                "total_score": item.total_score,
                "base_total_score": item.base_total_score,
                "market_regime_score_delta": item.market_regime_score_delta,
                "factor_scores": item.factor_scores,
                "reasons": item.reasons,
                "risk_warnings": item.risk_warnings,
                "confidence_notes": item.confidence_notes,
            }
            for item in report.candidates
        ],
        "excluded": [
            {
                "ticker": item.ticker,
                "excluded_reason": item.excluded_reason,
                "risk_warnings": item.risk_warnings,
                "confidence_notes": item.confidence_notes,
            }
            for item in report.excluded
        ],
    }


def report_to_markdown(report: Any, bundle: SnapshotBundle) -> str:
    lines = [
        "# yfinance 快照候選清單",
        "",
        f"- 來源：{bundle.source_name}",
        f"- 快照日期：{bundle.as_of}",
        f"- 抓取時間：{bundle.fetched_at}",
        f"- 輸入 {report.universe_size} 檔",
        f"- min_score：{report.min_score if report.min_score is not None else '未設定'}",
        f"- effective_min_score_source：{report.effective_min_score_source}",
        f"- retry_failed_count：{report.retry_failed_count}",
        f"- fetch_failed_count：{report.fetch_failed_count}",
        f"- sector_metadata_coverage：{report.sector_metadata_coverage}",
        f"- industry_metadata_coverage：{report.industry_metadata_coverage}",
        f"- metadata_fetch_failed_count：{report.metadata_fetch_failed_count}",
        f"- metadata_missing_count：{report.metadata_missing_count}",
        f"- sector_aware_status：{report.sector_aware_status}",
        f"- market_regime：{report.market_regime}",
        f"- market_regime_status：{report.market_regime_status}",
        f"- market_regime_signals：{json.dumps(report.market_regime_signals, ensure_ascii=False)}",
        f"- configured_composite_weights：{json.dumps(report.configured_composite_weights, ensure_ascii=False)}",
        f"- effective_composite_weights：{json.dumps(report.effective_composite_weights, ensure_ascii=False)}",
        f"- market_context：{json.dumps(report.market_context, ensure_ascii=False)}",
        f"- dedupe_removed_count：{report.dedupe_removed_count}",
        f"- 通過 {len(report.candidates)} 檔",
        f"- 剔除 {len(report.excluded)} 檔",
        "",
        "## 候選名單",
        "",
        "| 排名 | Ticker | 總分 | Base score | Regime delta | 基本面 | 動量 | 風險安全 | 主要理由 | 風險警示 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for index, item in enumerate(report.candidates, start=1):
        warnings = "；".join(item.risk_warnings) if item.risk_warnings else "無"
        reasons = "；".join(item.reasons)
        lines.append(
            "| {rank} | {ticker} | {total} | {base_total} | {regime_delta} | {fundamental} | {momentum} | {risk} | {reasons} | {warnings} |".format(
                rank=index,
                ticker=item.ticker,
                total=item.total_score if item.total_score is not None else "",
                base_total=item.base_total_score if item.base_total_score is not None else "",
                regime_delta=item.market_regime_score_delta if item.market_regime_score_delta is not None else "",
                fundamental=item.factor_scores.get("fundamental") or "",
                momentum=item.factor_scores.get("momentum") or "",
                risk=item.factor_scores.get("risk_safety") or "",
                reasons=reasons,
                warnings=warnings,
            )
        )
    if report.excluded:
        lines.extend(["", "## 剔除清單", "", "| Ticker | 原因 |", "| --- | --- |"])
        for item in report.excluded:
            lines.append(f"| {item.ticker} | {item.excluded_reason} |")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
