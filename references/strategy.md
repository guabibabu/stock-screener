# Strategy

Purpose: explain how scores are calculated. Canonical scoring logic lives in `scripts/us_stock_screener.py`; this file is the human-readable summary.
When you use the yfinance refresh flow, `data_age_days` and the fetch warnings should be carried into the report so stale data is obvious.

Default model: hybrid quantitative ranking for liquid US common stocks.
The second mode is `stop_checking_price`, a low-frequency quality screen inspired by "Stop Checking The Price" that emphasizes business quality, growth durability, capital efficiency, cash flow quality, balance-sheet risk, drawdown control, and data completeness.

## Output Shape

The default report is fixed:

- `候選排名`
- `每檔總分`
- `入選原因`
- `風險警示`
- `剔除原因`

## Hard Filters

- Exclude OTC, ETFs, ADRs, and halted names.
- Require `price >= 5`.
- Require `market_cap >= 2_000_000_000`.
- Require `avg_dollar_volume_20d >= 20_000_000`.
- Prefer `data_age_days <= 7`; warn when older than 3 days.
- Exclude thinly traded or stale names before scoring.

## Composite Weights

- `fundamental`: 40%
- `momentum`: 35%
- `risk_safety`: 25%

## Stop Checking Price Mode

Use this mode when you want to review a company snapshot instead of reacting to short-term price noise.

### Composite Weights

- `fundamental`: 55%
- `risk_safety`: 30%
- `momentum`: 15%

### Fundamental Subweights

- `quality`: 40%
- `growth`: 35%
- `valuation`: 15%
- `capital_efficiency`: 10%

### Momentum Subweights

- `long_term_trend`: 60%
- `relative_strength`: 25%
- `persistence`: 15%

### Risk Subweights

- `drawdown`: 30%
- `balance_sheet`: 25%
- `volatility`: 20%
- `earnings_stability`: 15%
- `liquidity_buffer`: 10%

### Quarterly Review

- Default behavior outside the quarterly review window is watchlist-only.
- Review windows are March, June, September, and December after the 15th.
- Use `--force-rebalance` if you want to override the watchlist-only behavior.

### Stop-Mode Penalties

The stop mode can apply soft penalties for:

- negative free cash flow
- negative operating margin or net margin
- high leverage
- dilution
- slowing growth
- deep drawdown
- high volatility
- stale data

### Stop-Mode Output

Each result can include:

- `company_snapshot`
- `confidence_score`
- `confidence_label`
- `penalties`
- `suggested_action`

## Fundamental Subweights

- `growth`: 40%
- `quality`: 35%
- `valuation`: 25%

## Momentum Subweights

- `relative_strength`: 45%
- `trend`: 35%
- `persistence`: 20%

## Risk Subweights

- `volatility`: 40%
- `beta`: 25%
- `drawdown`: 25%
- `liquidity_buffer`: 10%

## Default Reason Rules

Use short Chinese bullets that point to concrete evidence:

- Strong growth: revenue and EPS are both positive and above the medium-term threshold.
- Strong quality: margins and ROE are healthy, with manageable leverage.
- Reasonable valuation: P/E or P/S is not stretched relative to growth.
- Strong momentum: relative strength and price-versus-MA are both supportive.
- Controlled risk: beta, volatility, and drawdown are within acceptable bounds.

## Risk Warnings

Always flag the following when present:

- High beta
- Elevated volatility
- Deep drawdown
- High leverage
- Stale data
- Missing fundamental fields

## Tuning Knobs

- Tighten the universe by raising the market-cap or dollar-volume floor.
- Shift toward quality by increasing the fundamental weight.
- Shift toward tactical trading by increasing the momentum weight.
- Shift toward capital preservation by increasing the risk-safety weight.
- Add dividend preference by introducing a dividend-yield subfactor if needed later.
