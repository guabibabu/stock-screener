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
| `max_drawdown_252d` | 252-day max drawdown, in percent. |
| `debt_to_equity` | Debt-to-equity ratio. |
| `data_age_days` | Age of the latest snapshot in days. |

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
