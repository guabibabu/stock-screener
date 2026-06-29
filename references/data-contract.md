# Data Contract

Purpose: define the input shape for the screener. Canonical scoring behavior lives in `scripts/us_stock_screener.py`; the yfinance refresh flow lives in `scripts/fetch_yfinance_snapshot.py`.

Use one row per ticker. The screener accepts JSON arrays, JSON lines, or CSV files with these fields.

The canonical loader and scoring behavior live in `scripts/us_stock_screener.py`. The yfinance snapshot flow lives in `scripts/fetch_yfinance_snapshot.py`.

## Minimum Runnable Example

Start with `references/sample-watchlist.csv` if you only have tickers and want the auto-fetch flow.
Use `references/sample-universe.csv` if you already have a fully populated snapshot and want to skip fetching.

## Required Inputs

| Field | Meaning | Notes |
| --- | --- | --- |
| `ticker` | Ticker symbol | Required. |
| `price` | Latest share price | Required for liquidity and valuation checks. |
| `market_cap` | Market capitalization in USD | Required for the default large/mid-cap universe. |
| `avg_dollar_volume_20d` | 20-day average dollar volume in USD | Preferred liquidity field. |

## Auto-Fetch Input

The refresh helper accepts watchlists that only need:

- `ticker`

That watchlist is turned into a full snapshot before scoring.

## Market Context Sidecar

Market regime overlay uses a separate sidecar JSON. It is not embedded into the stock snapshot rows.

Required sidecar fields:

- `as_of_date`
- `spy_close`
- `spy_sma200`
- `qqq_close`
- `qqq_sma200`
- `vix_close`
- `breadth_above_200dma`
- `breadth_eligible_count`
- `market_context_source`

Optional helper field:

- `signals`

If the screener is run without an explicit sidecar, it must fall back to:

- `market_regime = neutral`
- `market_regime_status = insufficient_market_data`

## Historical Backtest Snapshot Format

Phase 4 backtesting only reads local historical snapshots from a folder of dated JSON files:

- `YYYY-MM-DD.json`

Fixed rules:

- The filename date is the only official backtest date source.
- If `metadata.as_of` is present, it must exactly match the filename date.
- If `metadata.requested_as_of` is present, it must exactly match the filename date.
- Backtests must not call yfinance or any other network source.
- Backtests must not backfill earlier snapshots with fields only known from later snapshots.

Minimum structure:

```json
{
  "metadata": {
    "as_of": "2026-01-31",
    "requested_as_of": "2026-01-31",
    "fetched_at": "2026-06-28T00:00:00Z",
    "source_data_end_date": "2026-01-31",
    "point_in_time_verified": true,
    "benchmarks": {
      "SPY": 610.25
    }
  },
  "records": [
    {
      "ticker": "AAPL",
      "price": 225.3,
      "market_cap": 3300000000000,
      "avg_dollar_volume_20d": 950000000
    }
  ]
}
```

If benchmark metadata is not embedded in the snapshot, the backtest can instead read a separate local `spy_prices.json`.

## Recommended Inputs

| Field | Meaning |
| --- | --- |
| `avg_volume_20d` | 20-day average share volume. Used with `price` when dollar volume is missing. |
| `revenue_growth_yoy` | Year-over-year revenue growth, in percent. |
| `eps_growth_yoy` | Year-over-year EPS growth, in percent. |
| `gross_margin` | Gross margin, in percent. |
| `operating_margin` | Operating margin, in percent. |
| `return_on_equity` | ROE, in percent. |
| `pe_ratio` | Trailing or forward P/E. |
| `ps_ratio` | Price-to-sales ratio. |
| `relative_strength_252d` | 0-100 relative strength percentile. |
| `price_vs_sma50_pct` | Distance from the 50-day moving average, in percent. |
| `price_vs_sma200_pct` | Distance from the 200-day moving average, in percent. |
| `beta` | Beta versus the market. |
| `volatility_63d` | 63-day realized volatility, in percent. |
| `max_drawdown_252d` | 252-day max drawdown, in positive percent points. Example: `15` = 15% drawdown. |
| `debt_to_equity` | Debt-to-equity ratio. |
| `data_age_days` | Age of the latest snapshot in days. |
| `price_data_age_days` | Age of price/volume data in days. Falls back to `data_age_days` if omitted. |
| `fundamental_data_age_days` | Age of financial/fundamental data in days. Falls back to `data_age_days` if omitted. |
| `shares_data_age_days` | Age of shares-outstanding data in days. Falls back to `data_age_days` if omitted. |

## Stop Checking Price Mode Optional Fields

This mode works best when the company snapshot is richer. These fields are optional for hybrid mode but strongly recommended for stop mode:

- `company_name`
- `sector`
- `industry`
- `gross_margin_ttm`
- `operating_margin_ttm`
- `net_margin_ttm`
- `roe`
- `roic`
- `free_cash_flow`
- `fcf_margin`
- `fcf_growth_yoy`
- `revenue_growth_3y_cagr`
- `forward_pe`
- `ev_to_ebitda`
- `peg_ratio`
- `net_debt_to_ebitda`
- `current_ratio`
- `interest_coverage`
- `shares_growth_yoy`
- `shares_growth_3y_cagr`
- `fcf_conversion`
- `operating_margin_3y_avg`
- `eps_positive_years_5y`
- `price_vs_200dma`
- `ma_200`
- `beta_1y`
- `volatility_1y`
- `max_drawdown_1y`
- `price_data_age_days`
- `fundamental_data_age_days`
- `shares_data_age_days`
- `financial_statement_date`
- `market_cap_timestamp`

## Stop Checking Price Output Notes

- `data_quality_flags` shows user-facing data issues that affect interpretation.
- `normalization_notes` shows debug-style field conversions, such as yfinance `debtToEquity` percentage normalization.
- `action_cap_reason` explains why the suggested action was capped.
- Missing `roic` with available `roe` is treated as a softer limitation than missing `free_cash_flow` or `shares_growth_yoy`.

## Percent And Drawdown Unit Contract

CSV / JSON snapshot inputs use percent points directly. They are not auto-converted by magnitude.

Examples:

- `0.5` = `0.5%`
- `1.5` = `1.5%`
- `15` = `15%`
- `150` = `150%`

Provider ratio fields must be converted explicitly by source-field mapping before they enter the snapshot contract.

Drawdown fields use positive drawdown magnitude:

- `max_drawdown_1y = 15` means a 15% drawdown
- `max_drawdown_1y = 40` means a 40% drawdown
- `max_drawdown_1y = 75` means a 75% drawdown

Signed legacy aliases may be normalized only when the field semantics explicitly imply signed drawdown:

- `drawdown_1y`
- `drawdown_252d`
- `max_drawdown`
- `drawdown`

Canonical drawdown fields must not silently flip sign:

- `max_drawdown_1y`
- `max_drawdown_252d`

## Report Diagnostics

All strategy modes can emit these report-level diagnostics:

- `ranking_style`
- `top_n_average_total_score`
- `top_n_average_fundamental_score`
- `top_n_average_momentum_score`
- `top_n_average_risk_safety_score`
- `high_risk_candidate_count`
- `expensive_candidate_count`
- `high_volatility_candidate_count`
- `deep_drawdown_candidate_count`
- `missing_data_candidate_count`
- `sector_aware_official_scoring`
- `sector_aware_shadow_mode`
- `sector_aware_preview_available_count`
- `sector_aware_preview_missing_count`
- `sector_aware_preview_coverage`
- `sector_aware_average_score_delta`
- `sector_aware_score_correlation_with_current`
- `sector_aware_rank_changed_count`
- `sector_aware_top_10_overlap`
- `sector_aware_top_10_overlap_total`
- `sector_aware_large_rank_change_count`
- `sector_aware_large_rank_change_threshold`
- `sector_aware_top_movers_up`
- `sector_aware_top_movers_down`
- `sector_aware_largest_movers`
- `sector_aware_sector_peer_used_count`
- `sector_aware_universe_fallback_count`
- `sector_aware_missing_sector_count`
- `sector_aware_average_peer_count`
- `sector_aware_min_peer_count`
- `sector_aware_max_peer_count`

Hybrid candidates can use `CANDIDATE_HIGH_RISK` or `CANDIDATE_DATA_LIMITED` when a name is still ranked but should not be read as a clean low-risk candidate.

## Sector-Aware Official Output

Sector-aware percentile scoring is now the official scoring layer. The `sector_relative_*` fields explain the official sector-aware score, and `legacy_*` fields preserve the old fixed-threshold score for comparison.

Each candidate can include:

- `base_total_score`
- `market_regime_score_delta`
- `sector_relative_score_preview`
- `sector_relative_rank_preview`
- `sector_relative_score_delta`
- `sector_relative_rank_delta`
- `sector_relative_factor_scores`
- `sector_relative_notes`
- `sector_relative_peer_source`
- `sector_relative_peer_count`
- `official_rank`
- `legacy_total_score`
- `legacy_raw_score`
- `legacy_adjusted_score`
- `legacy_fundamental_score`
- `legacy_momentum_score`
- `legacy_risk_safety_score`

`sector_relative_peer_source` can be:

- `sector`: same-sector peers were used.
- `industry`: same-industry peers were used.
- `universe_insufficient_peers`: industry and sector metadata were present but peer count was too small, so the full candidate universe was used.
- `universe_missing_metadata`: preview fallback when the stock is missing sector or industry metadata. The official score path still remains separate and may fall back to legacy scoring.
- `not_scored_sector_aware_disabled`: official sector-aware scoring was disabled because overall sector coverage was below the gate.

Official scoring provenance is separate from preview peer provenance:

- `official_score_source = sector_aware`
- `official_score_source = legacy_metadata_gate`
- `official_score_source = legacy_missing_metadata`

When `sector_aware_status = enabled` and a stock falls back to `official_score_source = legacy_missing_metadata`, it does not enter the official ranked candidate pool. Those names appear separately as `data_limited_candidates`, with `official_rank = null`.

## Review Metadata Output

All reports also expose manual-review metadata without changing score, rank, or action:

- report-level: `review_policy_version`, `review_mode`, `no_automatic_trading`, `review_summary`
- candidate-level: `review_required`, `review_priority`, `recommended_review_cadence`, `review_reasons`

`review_summary` counts are:

- `routine_weekly_count`
- `routine_quarterly_count`
- `prompt_manual_review_count`
- `data_review_required_count`

`data_limited_candidates` are included in these counts and must still carry prompt manual-review metadata.

`sector_relative_peer_count` is the number of records used for the percentile comparison. In universe fallback cases, this is the full candidate universe count.

`sector_relative_factor_scores` can include:

- `growth`
- `quality`
- `valuation`
- `fundamental`
- `momentum`
- `risk`
- `risk_safety`

## Backtest Output Contract

The research-only backtest script writes:

- `*.summary.csv`
- `*.periods.csv`
- `*.md`

Every CSV and Markdown report must include these fixed flags:

- `research_only = true`
- `not_point_in_time_accurate = true`
- `survivorship_bias_possible = true`
- `not_for_automated_trading = true`
- `missing_return_policy = invalidate_portfolio_period`

Required backtest disclosure fields:

- `missing_return_period_count`
- `missing_return_ticker_count`
- `missing_return_tickers`

When a holding is missing next-period price:

- that ticker is listed in `missing_return_tickers`
- the portfolio period return becomes `null`
- that period is excluded from CAGR, Sharpe, Sortino, volatility, drawdown, and hit-rate calculations

## Hard-Filter Flags

| Field | Meaning |
| --- | --- |
| `halted` | True if the symbol is halted or not actively trading. |
| `is_otc` | True if the security trades OTC. |
| `is_etf` | True if the security is an ETF. |
| `is_adr` | True if the security is an ADR. |
| `security_type` | Free-form descriptor; `common_stock` is ideal. |
| `exchange` | Exchange name such as NYSE or NASDAQ. |

## Accepted Synonyms

The loader accepts common aliases such as:

- `market_cap`, `marketcap`
- `avg_dollar_volume_20d`, `dollar_volume_20d`
- `avg_volume_20d`, `avg_daily_volume`, `volume_20d`
- `revenue_growth_yoy`, `revenue_growth`
- `eps_growth_yoy`, `eps_growth`
- `gross_margin`, `gross_margin_pct`
- `gross_margin_ttm`, `gross_margin_pct_ttm`
- `operating_margin`, `operating_margin_pct`
- `operating_margin_ttm`, `operating_margin_pct_ttm`
- `return_on_equity`, `roe`
- `pe_ratio`, `trailing_pe`
- `ps_ratio`, `price_to_sales`
- `forward_pe`
- `ev_to_ebitda`, `ev_ebitda`
- `peg_ratio`
- `relative_strength_252d`, `rs_252d_percentile`, `relative_strength_percentile`
- `price_vs_sma50_pct`, `above_sma50_pct`
- `price_vs_sma200_pct`, `above_sma200_pct`
- `price_vs_200dma`, `above_200dma_pct`
- `ma_200`, `sma200`, `200dma`
- `beta_1y`, `beta`
- `price_data_age_days`, `quote_data_age_days`, `market_data_age_days`, `data_age_days`
- `fundamental_data_age_days`, `financial_data_age_days`, `data_age_days`
- `shares_data_age_days`, `share_data_age_days`, `data_age_days`
- `volatility_63d`, `realized_volatility_63d`
- `volatility_1y`, `annualized_volatility_1y`, `volatility`
- `max_drawdown_252d`, `drawdown_252d`
- `max_drawdown_1y`, `drawdown_1y`, `max_drawdown`
- `debt_to_equity`, `de_ratio`
- `net_debt_to_ebitda`
- `current_ratio`
- `interest_coverage`
- `shares_growth_yoy`, `share_count_growth_yoy`
- `shares_growth_3y_cagr`
- `fcf_conversion`
- `operating_margin_3y_avg`
- `eps_positive_years_5y`

## Missing Data Rules

- Missing required inputs exclude the ticker if the screener cannot safely infer them.
- Missing recommended inputs do not crash the run.
- When a subfactor is missing, the screener renormalizes the remaining weights and marks the result as lower confidence.
- In stop mode, missing optional fundamentals lower `confidence_score` and can shift the suggested action to watchlist-only.
