# Strategy

Purpose: explain how scores are calculated. Canonical scoring logic lives in `scripts/us_stock_screener.py`; this file is the human-readable summary.
When you use the yfinance refresh flow, `price_data_age_days`, `fundamental_data_age_days`, `shares_data_age_days`, and fetch warnings should be carried into the report so stale data is obvious. Older snapshots can still use `data_age_days` as a fallback.

Default model: hybrid quantitative ranking for liquid US common stocks.
The second mode is `stop_checking_price`, a low-frequency quality screen inspired by "Stop Checking The Price" that emphasizes business quality, growth durability, capital efficiency, cash flow quality, balance-sheet risk, drawdown control, and data completeness.

## Output Shape

The default report is fixed:

- `候選排名`
- `每檔總分`
- `入選原因`
- `風險警示`
- `剔除原因`
- `ranking_style`
- `top_n` factor averages
- high-risk / expensive / high-volatility / drawdown / missing-data candidate counts

## Hard Filters

- Exclude OTC, ETFs, ADRs, and halted names.
- Require `price >= 5`.
- Require `market_cap >= 2_000_000_000`.
- Require `avg_dollar_volume_20d >= 20_000_000`.
- Prefer `price_data_age_days <= 7`; warn when older than 3 days.
- Treat `fundamental_data_age_days` and `shares_data_age_days` as quality warnings rather than automatic price/liquidity exclusions.
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
- `data_quality_score`
- `data_quality_flags`
- `normalization_notes`
- `action_cap_reason`
- `penalties`
- `suggested_action`

### Stop-Mode Action Caps

- Missing `free_cash_flow` caps the action at `WATCHLIST`.
- Missing `shares_growth_yoy` caps the action at `WATCHLIST`.
- Missing `roic` with available `roe` caps the action at `WATCHLIST_HIGH_QUALITY`, because ROE can serve as a weaker capital-efficiency proxy.
- Missing both `roic` and `roe` caps the action at `WATCHLIST`.
- Low-severity price-history gaps such as 251/252 observations are hidden from default candidate flags; shorter histories remain visible.
- Debt-to-equity unit conversions are reported as `normalization_notes` instead of default data-quality warnings.

### Stop-Mode Score Floor

- The default `min_score` is unset.
- Use `--min-score` only when you explicitly want a fixed score cutoff.
- This keeps stop mode useful as a manual review list instead of hiding candidates during weak or incomplete-data market snapshots.

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

## Ranking Diagnostics

Reports include a lightweight diagnostic layer that does not change ranking or scores:

- `ranking_style`: `momentum_driven`, `quality_driven`, `defensive`, or `balanced`
- `top_n_average_total_score`
- `top_n_average_fundamental_score`
- `top_n_average_momentum_score`
- `top_n_average_risk_safety_score`
- `high_risk_candidate_count`
- `expensive_candidate_count`
- `high_volatility_candidate_count`
- `deep_drawdown_candidate_count`
- `missing_data_candidate_count`

Hybrid candidates can use safer action labels:

- `CANDIDATE`: regular candidate
- `CANDIDATE_HIGH_RISK`: score is acceptable but risk-safety is low
- `CANDIDATE_DATA_LIMITED`: candidate has missing factor fields

## Percentile Scoring Utilities

The codebase includes reusable percentile scoring helpers for shadow-mode and sector-aware scoring. These helpers are not wired into the live hybrid or stop-mode score, so current rankings and actions are unchanged.

- `winsorize_value`
- `winsorize_series`
- `percentile_rank`
- `score_higher_is_better`
- `score_lower_is_better`
- `score_with_missing_policy`
- `safe_zscore`

Supported missing-data policies:

- `ignore`: return `None` so the caller can reweight remaining factors
- `neutral`: return 50
- `zero`: return 0
- `penalize`: return a low configurable score, default 25

## Sector-Aware Shadow Preview

Reports now include a sector-relative preview layer. This is a shadow score only:

- It does not change `total_score`.
- It does not change formal ranking.
- It does not change `suggested_action`.
- It is used to observe how a sector-aware percentile model would compare with the current model.

Candidate-level preview fields:

- `sector_relative_score_preview`
- `sector_relative_rank_preview`
- `sector_relative_score_delta`
- `sector_relative_rank_delta`
- `sector_relative_factor_scores`
- `sector_relative_notes`

Report-level preview fields:

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

Phase 2C.5 diagnostics:

- `sector_aware_preview_coverage`: share of displayed candidates with usable preview scores.
- `sector_aware_score_correlation_with_current`: Pearson correlation between official score and preview score.
- `sector_aware_top_10_overlap`: overlap count between official top 10 and preview top 10.
- `sector_aware_large_rank_change_count`: count of names whose preview rank moved by at least the configured threshold.
- `sector_aware_largest_movers`: largest rank movers with preview factor breakdown and notes.

Initial preview factors:

- Higher is better: `revenue_growth_yoy`, `eps_growth_yoy`, `gross_margin`, `operating_margin`, `return_on_equity`, `relative_strength_252d`, `price_vs_sma200_pct`, `avg_dollar_volume_20d`
- Lower is better: `pe_ratio`, `ps_ratio`, `volatility_63d`, `beta`, `max_drawdown_252d`

Peer selection:

- Use same-sector percentile when sector peer count is at least 30.
- Otherwise fall back to the full candidate universe.
- Missing fields are ignored and reweighted inside the preview; they are reported in `sector_relative_notes`.

## Tuning Knobs

- Tighten the universe by raising the market-cap or dollar-volume floor.
- Shift toward quality by increasing the fundamental weight.
- Shift toward tactical trading by increasing the momentum weight.
- Shift toward capital preservation by increasing the risk-safety weight.
- Add dividend preference by introducing a dividend-yield subfactor if needed later.
