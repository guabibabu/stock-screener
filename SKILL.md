---
name: us-stock-screener
description: Daily US equity screening for large- and mid-cap liquid common stocks. Use when Codex needs to rank a provided US stock universe, exclude OTC/ETF/ADR/low-liquidity names, combine fundamental, momentum, and risk factors, or produce Chinese candidate lists with reasons and warnings for manual review.
---

# US Stock Screener

Canonical source: the installed skill at `~/.codex/skills/us-stock-screener/`. The copies under `~/plugins/` and the workspace are mirrors for packaging and editing.

## Quick Start

1. First-time use: open `launch_us_stock_screener.command` and choose a ticker list.
2. If the desktop GUI feels laggy, open `launch_us_stock_screener_web.command` to use the local browser version.
3. Fastest auto-fetch path: use `references/sample-watchlist.csv`, click `更新並篩選`, and let the app build a fresh snapshot and ranking for you.
4. Manual snapshot path: use `references/sample-universe.csv` if you already have price, market cap, and liquidity fields.
5. Minimum required columns for the screener: `ticker`, `price`, `market_cap`, `avg_dollar_volume_20d` or `avg_volume_20d`.
6. Strategy modes: `hybrid` for the default mixed ranking model, or `stop_checking_price` for the low-frequency quality screen.
7. Default output: ranked candidates, total score, sector-aware preview, factor scores, reasons, risk warnings, confidence notes, and a clear exclusion list.
8. If data is missing, the screener names the missing fields and lowers confidence instead of guessing.

## What Each File Does

- `SKILL.md`: how to use the tool end to end.
- `references/data-contract.md`: what the input should look like.
- `references/strategy.md`: how the scores are calculated.
- `scripts/fetch_yfinance_snapshot.py`: turn a watchlist into a fresh snapshot.

## Workflow

1. Load the day's input snapshot or upstream feed.
2. If you only have tickers, run the yfinance snapshot helper first.
3. Run `scripts/us_stock_screener.py` on common US stocks only.
4. Apply the hard filters in `references/strategy.md`.
5. Score surviving names with the selected strategy mode.
6. Return the top candidates in Chinese with score, reasons, warnings, confidence, and suggested action when available.

## Scope

- Only use this for US large- and mid-cap ordinary shares with real liquidity.
- Exclude OTC, ETFs, ADRs, halted names, penny stocks, and thinly traded issues.

## Outputs

- Candidate list ordered by total score.
- Sector-aware preview fields for diagnostics only; these do not change the official ranking or action label.
- Factor breakdown: fundamental, growth, quality, valuation, momentum, risk, liquidity, and confidence when available.
- Reason bullets in Chinese.
- Risk warnings for volatility, leverage, drawdown, stale data, and missing fundamentals.
- Exclusion list with a short reason when names fail a hard filter.

## Tuning

- Adjust thresholds and weights in `references/strategy.md`.
- For a watchlist-only mode, feed a smaller universe file and keep the same scorer.
- For weekly mode, reuse the same script but point it at the latest weekly snapshot.

## Resources

- `scripts/us_stock_screener.py`: CLI and library screener.
- `scripts/us_stock_screener_gui.py`: Beginner-friendly GUI with the watchlist-to-snapshot flow.
- `scripts/us_stock_screener_web.py`: Local browser UI that reuses the same screener core.
- `scripts/fetch_yfinance_snapshot.py`: yfinance snapshot fetcher and CLI.
- `launch_us_stock_screener.command`: Double-click launcher for macOS.
- `launch_us_stock_screener_web.command`: Double-click launcher for the local web version.
- `scripts/test_us_stock_screener.py`: Deterministic dry-run tests with mock data.
- `references/data-contract.md`: Expected input fields and synonyms.
- `references/strategy.md`: Default filters, weights, and tuning rules.
