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
- `review_policy_version`
- `review_mode`
- `review_summary`

## Manual Review Discipline

This tool is fixed as a manual decision-support workflow. It does not emit automatic trading instructions, and review metadata is report-layer guidance only.

Report-level fields:

- `review_policy_version = v1`
- `review_mode = manual_decision_support`
- `no_automatic_trading = true`
- `review_summary`

Candidate-level fields:

- `review_required`
- `review_priority`
- `recommended_review_cadence`
- `review_reasons`

Rules:

- Hybrid `CANDIDATE` => routine weekly review, reason `hybrid_weekly_candidate_review`
- Hybrid `CANDIDATE_HIGH_RISK` => prompt manual review, reason `high_risk_candidate`
- Hybrid `CANDIDATE_DATA_LIMITED` => prompt manual review, reason `data_limited_candidate`
- Stop `WATCHLIST` / `WATCHLIST_HIGH_QUALITY` => routine quarterly review, reason `stop_mode_quarterly_review`
- Stop `BUY_CANDIDATE` / `HOLD_OR_REVIEW` => still routine quarterly review, reason `stop_mode_quarterly_review`
- Stop `WATCHLIST_DATA_INSUFFICIENT` => prompt manual review, reason `data_insufficient`
- Any non-`AVOID` / non-`EXCLUDE` candidate with `action_cap_reason` => prompt manual review, reason `action_cap_requires_manual_review`
- Any non-`AVOID` / non-`EXCLUDE` candidate with `risk_safety_score < 40` => prompt manual review, reason `low_risk_safety_score`
- `data_limited_candidates` always stay outside the official ranked pool and must still be marked for prompt manual review with `data_limited_candidate`
- `AVOID` / `EXCLUDE` => `review_required = false`

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

## Market Regime Overlay

Market regime is a report-level overlay. It does not add new factors, does not change hard filters, and does not replace the official score source selected by the Phase 2D metadata gate.

Required market context sidecar fields:

- `as_of_date`
- `spy_close`
- `spy_sma200`
- `qqq_close`
- `qqq_sma200`
- `vix_close`
- `breadth_above_200dma`
- `breadth_eligible_count`
- `market_context_source`

Signal rules:

- `SPY > 200DMA` => risk-on
- `SPY < 200DMA` => risk-off
- `QQQ > 200DMA` => risk-on
- `QQQ < 200DMA` => risk-off
- `VIX < 20` => risk-on
- `VIX >= 25` => risk-off
- `0.40 < breadth < 0.60` => neutral

Classification:

- fewer than 3 valid signals => `market_regime = neutral`, `market_regime_status = insufficient_market_data`
- 3 or more risk-on signals and no risk-off signals => `risk_on`
- 3 or more risk-off signals and no risk-on signals => `risk_off`
- otherwise => `neutral`

Hybrid effective composite weights:

- `risk_on`: momentum +5 pts, risk_safety -5 pts
- `neutral`: no change
- `risk_off`: fundamental -5 pts, momentum -5 pts, risk_safety +10 pts

Stop effective composite weights:

- `risk_on`: no change
- `neutral`: no change
- `risk_off`: momentum -5 pts, risk_safety +5 pts

Score provenance:

- `base_total_score` = official score before regime overlay
- `total_score` = final score after regime overlay
- `market_regime_score_delta` = `total_score - base_total_score`

For stop mode, the regime score must still go through the same penalty, confidence multiplier, and action-cap path as the base official score.

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
- Leave it blank = no minimum score; manual review only.
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

The codebase includes reusable percentile scoring helpers used by the official sector-aware scoring layer and its diagnostics.

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

## Sector-Aware Official Scoring

Phase 2D makes sector-relative percentile scoring the official factor model:

- It now changes `total_score`.
- It can change formal ranking.
- It can change `suggested_action`.
- `legacy_*` fields are retained for comparison only.

## Stop Drawdown Contract

Stop mode treats drawdown as a positive drawdown magnitude:

- `15` = 15% drawdown
- `40` = 40% drawdown
- `75` = 75% drawdown

Scoring:

- `drawdown_score = score_low_better(drawdown, 15.0, 50.0)`
- deep-drawdown penalty triggers when `drawdown > 40.0`
- It can change `suggested_action` through the recalculated risk/action layer.
- The previous fixed-threshold score is preserved in `legacy_*` fields for debug and comparison.

Candidate-level sector-aware fields:

## Backtest / Validation Framework

Phase 4 backtesting is research-only. It exists to compare ranking behavior over historical local snapshots. It is not a trading engine and must not connect to broker APIs or intraday feeds.

### Historical Inputs

- Use local dated snapshots only: `YYYY-MM-DD.json`
- The filename date is the backtest date
- If `metadata.as_of` exists, it must match the filename date
- No network calls are allowed during backtest runs

### Rebalance Rules

Hybrid:

- monthly rebalance
- use the last available snapshot in each month
- evaluate `top 20`, `top 50`, and `top decile`
- `top decile = max(1, floor(eligible_candidate_count * 0.10))`

Stop Checking Price:

- quarterly rebalance
- use the last available snapshot in each quarter
- evaluate `top 20` and `top 50`

### Return Rules

- formation uses the official ranking from the formation snapshot only
- holdings are equal weight
- return is close-to-close from the formation snapshot to the next rebalance snapshot
- no future snapshot may be used to re-score the formation portfolio

### Missing Next-Period Price Policy

If a holding does not have next-period price:

- mark that ticker as `missing_next_period_price`
- set that ticker's return to `null`
- set the entire portfolio period return to `null`
- exclude that period from CAGR, Sharpe, Sortino, volatility, drawdown, and hit-rate metrics

### Benchmarks

SPY:

- use local price only
- source can be snapshot benchmark metadata or a separate local `spy_prices.json`
- missing SPY price leaves that benchmark period as `null`

Equal-weight universe:

- use all hard-pass names from the formation snapshot
- include only tickers that also have next-period price
- disclose per-period eligible count

### Fixed Metrics

- `risk_free_rate = 0`
- Hybrid annualization factor = `12`
- Stop annualization factor = `4`
- `turnover = 0.5 * sum(abs(current_weight - previous_weight))`
- `top_holdings_concentration = min(10, holding_count) / holding_count`

### Required Report Flags

- `research_only = true`
- `not_point_in_time_accurate = true`
- `survivorship_bias_possible = true`
- `not_for_automated_trading = true`
- `missing_return_policy = invalidate_portfolio_period`

- `sector_relative_score_preview`
- `sector_relative_rank_preview`
- `sector_relative_score_delta`
- `sector_relative_rank_delta`
- `sector_relative_factor_scores`
- `sector_relative_notes`
- `sector_relative_peer_source`
- `sector_relative_peer_count`
- `legacy_total_score`
- `legacy_raw_score`
- `legacy_adjusted_score`
- `legacy_fundamental_score`
- `legacy_momentum_score`
- `legacy_risk_safety_score`

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
- `sector_aware_sector_peer_used_count`
- `sector_aware_universe_fallback_count`
- `sector_aware_missing_sector_count`
- `sector_aware_average_peer_count`
- `sector_aware_min_peer_count`
- `sector_aware_max_peer_count`

Phase 2C.5 diagnostics:

- `sector_aware_preview_coverage`: share of displayed candidates with usable preview scores.
- `sector_aware_score_correlation_with_current`: Pearson correlation between official score and preview score.
- `sector_aware_top_10_overlap`: overlap count between official top 10 and preview top 10.
- `sector_aware_large_rank_change_count`: count of names whose preview rank moved by at least the configured threshold.
- `sector_aware_largest_movers`: largest rank movers with preview factor breakdown and notes.

Phase 2C.6 peer provenance diagnostics:

- `sector_relative_peer_source`: `industry`, `sector`, `universe_insufficient_peers`, `universe_missing_metadata`, or `not_scored_sector_aware_disabled`.
- `sector_relative_peer_count`: number of records used for the percentile comparison. Universe fallback uses the full displayed candidate universe count.
- `sector_aware_sector_peer_used_count`: candidates whose preview used same-sector peers.
- `sector_aware_universe_fallback_count`: candidates with industry and sector metadata, but too few peers, so preview fell back to the universe.
- `sector_aware_missing_sector_count`: preview-only count of candidates missing sector or industry metadata.
- `sector_aware_average_peer_count`, `sector_aware_min_peer_count`, and `sector_aware_max_peer_count`: peer-count distribution for available preview scores.

When `sector_aware_status = enabled`, names with `official_score_source = legacy_missing_metadata` stay on the legacy factor path, can still receive market-regime overlay on that legacy path, but do not enter the official ranked pool. They are shown separately as `data_limited_candidates`.

Official sector-aware factors:

- Higher is better: `revenue_growth_yoy`, `eps_growth_yoy`, `gross_margin`, `operating_margin`, `return_on_equity`, `relative_strength_252d`, `price_vs_sma200_pct`, `avg_dollar_volume_20d`
- Lower is better: `pe_ratio`, `ps_ratio`, `volatility_63d`, `beta`, `max_drawdown_252d`

Peer selection:

- Use same-industry percentile when industry peer count is at least 30.
- Otherwise use same-sector percentile when sector peer count is at least 30.
- Otherwise fall back to the full candidate universe.
- Reports show the peer source and peer count so users can tell whether the score is truly industry/sector-relative or mostly universe fallback.
- Missing fields are ignored and reweighted inside the sector-aware score; they are reported in `sector_relative_notes`.
- Peer values are winsorized from p5 to p95 before percentile scoring.

## Tuning Knobs

- Tighten the universe by raising the market-cap or dollar-volume floor.
- Shift toward quality by increasing the fundamental weight.
- Shift toward tactical trading by increasing the momentum weight.
- Shift toward capital preservation by increasing the risk-safety weight.
- Add dividend preference by introducing a dividend-yield subfactor if needed later.
