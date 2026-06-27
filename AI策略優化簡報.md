# 美股自動選股助手 Strategy 優化簡報

日期：2026-04-30

用途：這份文件是給另一個 AI 或策略研究者閱讀的背景資料。目標是讓對方理解目前程式的選股策略，然後提出更好的策略設計、權重、因子、風險控制與測試方案。

請注意：這個工具是「決策輔助工具」，不是自動交易系統，不會自動下單，也不應輸出保證買賣訊號。

## 1. 程式定位

這是一個美股選股輔助工具，主要用於：

- 從美股清單中篩選大型 / 中大型、高流動性的普通股。
- 排除 OTC、ETF、ADR、停牌、低價、低市值、低流動性與資料過舊標的。
- 對剩下的股票做量化打分。
- 輸出候選排名、總分、分項分數、入選理由、風險提醒、資料缺口、剔除原因。
- 支援每日 / 週期性人工審查，不做自動下單。

目前有兩個策略模式：

- `hybrid`：日常混合量化篩選。
- `stop_checking_price`：長期、低頻、品質導向篩選。

## 2. 主要程式與文件位置

- 核心評分器：`scripts/us_stock_screener.py`
- yfinance 抓資料：`scripts/fetch_yfinance_snapshot.py`
- 桌面 GUI：`scripts/us_stock_screener_gui.py`
- 本機網頁版：`scripts/us_stock_screener_web.py`
- 策略說明：`references/strategy.md`
- 資料格式：`references/data-contract.md`
- 測試：`scripts/test_us_stock_screener.py`

目前核心策略的真實來源是 `scripts/us_stock_screener.py`。

## 3. 資料來源

資料可以來自兩種方式：

1. 使用者提供完整 snapshot。
2. 使用者只提供 ticker 清單，程式透過 `yfinance` 抓 Yahoo Finance 可取得資料。

yfinance 會嘗試取得：

- 價格
- 歷史價格
- 成交量
- 市值
- 公司基本資訊
- 部分財務與估值欄位
- 現金流資料
- shares outstanding 歷史資料

限制：

- yfinance 不是專業即時行情源。
- 價格可能延遲。
- 財報與估值資料可能缺漏或不同步。
- 部分欄位可能抓不到，例如 `roic`、`free_cash_flow`、`shares_growth_yoy`。
- 抓取失敗會標記為 fetch failed，不應被誤判為公司基本面差。

## 4. 最小必要輸入欄位

如果要直接評分，至少需要：

- `ticker`
- `price`
- `market_cap`
- `avg_dollar_volume_20d`

如果沒有 `avg_dollar_volume_20d`，可用：

- `avg_volume_20d + price`

如果只有 `ticker`，必須先透過 yfinance 產生 snapshot。

## 5. 通用硬性剔除條件

兩種策略模式都會先做硬性剔除：

- 排除停牌：`halted == True`
- 排除 OTC：`is_otc == True`
- 排除 ETF：`is_etf == True`
- 排除 ADR：`is_adr == True`
- 排除 preferred stock
- 排除交易所名稱含 OTC 的標的
- 股價必須 `price >= 5`
- 市值必須 `market_cap >= 2_000_000_000`
- 20 日均成交額必須 `avg_dollar_volume_20d >= 20_000_000`
- 若 `price_data_age_days > 7`，視為價格資料過舊並排除；舊 snapshot 可用 `data_age_days` fallback

硬性剔除報告會顯示：

- `category`
- `field`
- `raw_value`
- `normalized_value`
- `threshold`
- `reason`

## 6. 同公司去重

目前兩種策略模式都啟用同公司去重。

以下 ticker 只保留一檔：

- `GOOG / GOOGL`
- `FOX / FOXA`
- `NWS / NWSA`
- `BRK-A / BRK-B`
- `BRK.A / BRK.B`

去重邏輯會保留分數較高者。Stop mode 會優先看 `adjusted_score`，再看 `confidence_score`。

## 7. Hybrid 模式

### 7.1 使用目的

`hybrid` 是日常混合量化篩選模式，適合每日或每週初篩。

它同時重視：

- 基本面
- 動量
- 風險安全

### 7.2 Composite Weights

```text
fundamental = 40%
momentum = 35%
risk_safety = 25%
```

### 7.3 Fundamental Subweights

```text
growth = 40%
quality = 35%
valuation = 25%
```

目前大致使用：

- 成長：`revenue_growth_yoy`、`eps_growth_yoy`
- 品質：`gross_margin`、`operating_margin`、`return_on_equity`
- 估值：`pe_ratio`、`ps_ratio`

### 7.4 Momentum Subweights

```text
relative_strength = 45%
trend = 35%
persistence = 20%
```

目前大致使用：

- `relative_strength_252d`
- `price_vs_sma50_pct`
- `price_vs_sma200_pct`

### 7.5 Risk Subweights

```text
volatility = 40%
beta = 25%
drawdown = 25%
liquidity_buffer = 10%
```

目前大致使用：

- `volatility_63d`
- `beta`
- `max_drawdown_252d`
- `avg_dollar_volume_20d`

### 7.6 Hybrid 輸出

Hybrid 候選通常輸出：

- `total_score`
- `fundamental_score`
- `momentum_score`
- `risk_safety_score`
- `reasons`
- `risk_warnings`
- `suggested_action = CANDIDATE`

Hybrid 預設沒有 `min_score` 門檻，報告顯示 `min_score：未設定`。

## 8. Stop Checking Price 模式

### 8.1 使用目的

`stop_checking_price` 是長期、低頻、品質導向模式。

它的設計目標是降低短期價格噪音，重視公司快照：

- business quality
- growth durability
- capital efficiency
- cash flow quality
- balance sheet risk
- drawdown control
- data completeness
- low trading frequency

### 8.2 Composite Weights

```text
fundamental = 55%
risk_safety = 30%
momentum = 15%
```

### 8.3 Fundamental Subweights

```text
quality = 40%
growth = 35%
valuation = 15%
capital_efficiency = 10%
```

Quality 使用：

- `gross_margin_ttm`
- `operating_margin_ttm`
- `free_cash_flow`
- `roe`
- `debt_to_equity`

Growth 使用：

- `revenue_growth_yoy`
- `eps_growth_yoy`
- `fcf_growth_yoy`
- `revenue_growth_3y_cagr`

Valuation 使用：

- `pe_ratio`
- `forward_pe`
- `ps_ratio`
- `ev_to_ebitda`
- `peg_ratio`

Capital efficiency 使用：

- `roic`
- `roe`
- `fcf_conversion`
- `shares_growth_yoy`

### 8.4 Momentum Subweights

```text
long_term_trend = 60%
relative_strength = 25%
persistence = 15%
```

Stop mode 的 momentum 是防守型，不應過度獎勵短期暴漲。

Long-term trend 主要看：

- `price_vs_200dma`
- 或 `price / ma_200 - 1`

### 8.5 Risk Subweights

```text
drawdown = 30%
balance_sheet = 25%
volatility = 20%
earnings_stability = 15%
liquidity_buffer = 10%
```

Balance sheet 使用：

- `debt_to_equity`
- `net_debt_to_ebitda`
- `current_ratio`
- `interest_coverage`

Earnings stability 使用：

- `eps_growth_yoy`
- `revenue_growth_yoy`
- `operating_margin_ttm`
- `operating_margin_3y_avg`
- `eps_positive_years_5y`

## 9. Stop Mode 額外硬性剔除

Stop mode 有一些額外硬性剔除，但只在欄位可取得時使用：

- `price_data_age_days > 30`：價格資料超過 30 天，過舊
- `operating_margin_ttm < -0.20`：營業利益率嚴重為負
- `debt_to_equity_normalized > 10` 且不是特殊產業：負債權益比極端異常
- `shares_growth_yoy > 0.20`：股本稀釋嚴重

金融、銀行、保險、REIT、Utilities 會使用 sector-aware debt logic，不直接用一般負債權益比門檻硬剔除。

## 10. debt_to_equity 單位處理

yfinance 的 `debtToEquity` 可能是百分比格式。

目前邏輯：

- 保留 `debt_to_equity_raw`
- 新增 `debt_to_equity_normalized`
- 若 raw value > 10，視為百分比格式並除以 100
- 所有風險判斷使用 normalized value

例：

```text
raw = 280
normalized = 2.8
```

## 11. Stop Mode Soft Penalty

Stop mode 會套用 soft penalty，總扣分上限為 25 分。

目前 penalty 包括：

- `free_cash_flow < 0`：扣 8
- `operating_margin_ttm < 0`：扣 10
- `net_margin_ttm < 0`：扣 6
- `pe_ratio > 50`：扣 6
- `forward_pe > 45`：扣 5
- `ps_ratio > 20`：扣 6
- `ev_to_ebitda > 35`：扣 6
- `peg_ratio > 3`：扣 4
- `debt_to_equity > 2.0`：扣 8
- `net_debt_to_ebitda > 4.0`：扣 8
- `shares_growth_yoy > 0.05`：扣 6
- `shares_growth_3y_cagr > 0.05`：扣 8
- `revenue_growth_yoy < -0.05`：扣 6
- `eps_growth_yoy < -0.10`：扣 6
- `max_drawdown_1y < -0.40`：扣 8
- `volatility_1y > 0.80`：扣 6
- `price_data_age_days > 7`：扣 8
- `price_data_age_days > 3`：扣 4

資料過舊不重複扣分，超過 7 天只扣較大的 stale-data penalty。

## 12. Stop Mode Confidence Score

Stop mode 會計算資料完整度：

```text
confidence_score = available_required_fields / total_required_fields
```

Required fields：

- `ticker`
- `price`
- `market_cap`
- `avg_dollar_volume_20d`
- `revenue_growth_yoy`
- `eps_growth_yoy`
- `gross_margin_ttm`
- `operating_margin_ttm`
- `roe`
- `roic`
- `free_cash_flow`
- `debt_to_equity`
- `shares_growth_yoy`
- `pe_ratio`
- `ps_ratio`
- `max_drawdown_1y`
- `volatility_1y`
- `price_vs_200dma`
- `price_data_age_days`

Confidence labels：

```text
>= 0.85 high
>= 0.70 medium
>= 0.55 low
< 0.55 very_low
```

Stop mode final score formula：

```text
raw_score = fundamental * 0.55 + risk_safety * 0.30 + momentum * 0.15
score_after_penalty = max(0, raw_score - penalty_score)
confidence_multiplier = 0.75 + 0.25 * confidence_score
final_score = score_after_penalty * confidence_multiplier
final_score clipped to 0-100
```

若缺少 action-critical fields：

- `free_cash_flow`
- `shares_growth_yoy`

則 suggested action 不允許高於 `WATCHLIST`。

`roic` 目前採較軟的限制：

- 缺 `roic` 但有 `roe`：最多允許到 `WATCHLIST_HIGH_QUALITY`
- 缺 `roic` 且缺 `roe`：最多只允許到 `WATCHLIST`

## 13. Stop Mode Suggested Action

Stop mode 不是每日買賣訊號，而是低頻審查。

季度審查期：

- 3 月、6 月、9 月、12 月
- 且日期 >= 15 日

若不在季度審查期，且沒有 force rebalance：

- score >= 75：`WATCHLIST_HIGH_QUALITY`
- score >= 65：`WATCHLIST`
- 否則：`AVOID`

若在季度審查期或強制季度檢查：

- score >= 82 且 confidence >= 0.70：`BUY_CANDIDATE`
- score >= 72 且 confidence >= 0.65：`HOLD_OR_REVIEW`
- score >= 62：`WATCHLIST`
- 否則：`AVOID`

若資料完整度太低：

- confidence < 0.55：`WATCHLIST_DATA_INSUFFICIENT`

若硬性剔除：

- `EXCLUDE`

## 14. Stop Mode 預設 min_score

目前預設：

- `hybrid`：不設最低分，`min_score = None`
- `stop_checking_price`：不設最低分，`min_score = None`

使用者可以手動指定 `min_score` 覆蓋預設值。

## 15. 輸出報告包含什麼

報告摘要：

- `strategy_mode`
- `review_mode`
- `universe_size`
- `hard_pass_count`
- `candidate_count`
- `hard_exclusion count`
- `soft_penalty count`
- `missing_data count`
- `retry_failed_count`
- `fetch_failed_count`
- `dedupe_removed_count`
- `min_score`
- `effective_min_score_source`
- `top_n`
- `dedupe_company`
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

候選股票：

- `ticker`
- `total_score`
- `raw_score`
- `adjusted_score`
- `final_score`
- `fundamental_score`
- `momentum_score`
- `risk_safety_score`
- `factor_scores`
- `reasons`
- `risk_warnings`
- `confidence_notes`
- `penalties`
- `suggested_action`
- `company_snapshot`

剔除股票：

- `ticker`
- `excluded_reason`
- `exclusion_reasons`
- `exclusion_details`

## 16. 目前策略的已知限制

請改善策略時特別注意這些限制：

1. yfinance 資料缺漏多，尤其是 ROIC、shares outstanding、free cash flow。
2. 現在沒有完整回測框架，策略好壞主要靠單次 snapshot 和人工判斷。
3. 估值門檻沒有充分 sector-aware，例如高成長科技、金融、能源、公用事業應有不同估值尺度。
4. debt_to_equity 已有 sector-aware 初步處理，但仍較粗。
5. momentum 目前較簡化，沒有市場 regime、相對行業強度、波動調整後動量。
6. risk model 主要用 beta、volatility、drawdown，沒有 tail risk、earnings revision、liquidity shock。
7. Stop mode 已移除預設 85 分門檻，但仍需要 top percentile / top_n 等更穩定的候選控制方式。
8. soft penalty 門檻固定，沒有依產業分位數或市場環境動態調整。
9. 沒有明確處理大型科技股高估值但高品質的 trade-off。
10. 沒有 portfolio construction、position sizing、sell discipline 或 rebalance rules。

## 17. 請另一個 AI 幫忙改善時的任務

請基於上述策略，提出一版更好的 strategy vNext。

請回答以下內容：

1. 是否保留 `hybrid` 和 `stop_checking_price` 兩種模式？
2. 每個模式的 composite weights 應如何調整？
3. 每個模式的 subfactor weights 應如何調整？
4. 哪些因子應新增、刪除或降權？
5. 哪些硬性剔除條件應調整？
6. 哪些 soft penalty 應改成 sector-aware 或 percentile-based？
7. Stop mode 是否應改用 top percentile / top_n 取代固定 `min_score`？
8. 如何改善資料缺失時的 scoring 和 confidence？
9. 如何讓 yfinance 資料限制下的結果更可靠？
10. 應新增哪些測試與 mock data？
11. 是否需要加入 market regime filter？
12. 是否需要加入 sector-relative scoring？
13. 是否需要加入 sell / review discipline，但不做自動下單？

## 17.1 目前已準備好的 Phase 2B 工具層

目前已新增 percentile / winsorization 工具，但尚未接入正式分數：

- `winsorize_value`
- `winsorize_series`
- `percentile_rank`
- `score_higher_is_better`
- `score_lower_is_better`
- `score_with_missing_policy`
- `safe_zscore`

支援 missing policy：

- `ignore`
- `neutral`
- `zero`
- `penalize`

請注意：這些函式目前只是工具層，正式 hybrid / stop mode 排名尚未改成 percentile scoring。

## 17.2 目前已完成的 Phase 2C sector-aware shadow mode

目前已新增 sector-aware preview，但尚未接入正式分數：

- `sector_relative_score_preview`
- `sector_relative_rank_preview`
- `sector_relative_score_delta`
- `sector_relative_rank_delta`
- `sector_relative_factor_scores`
- `sector_relative_notes`
- `sector_relative_peer_source`
- `sector_relative_peer_count`
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

這一層只用來觀察：

```text
如果用同產業 percentile scoring，候選股分數與排名會怎麼變？
```

目前不會改變：

- 正式 `total_score`
- 正式候選排序
- `suggested_action`
- hard filters
- soft penalties

初版 preview 使用同 sector peers；若同產業候選數少於 30，fallback 到全候選 universe。缺欄位不直接扣分，只在 preview 內忽略並寫入 notes。

Phase 2C.5 又補上 preview diagnostics：

```text
coverage
score correlation with current score
official top 10 vs preview top 10 overlap
large rank movement count
largest movers with factor explanation
```

Phase 2C.6 又補上 peer provenance diagnostics：

```text
sector peer used count
universe fallback count
missing sector count
average / min / max peer count
candidate peer source
candidate peer count
```

這能判斷 sector-aware preview 是否真的使用同產業同儕，或只是因同產業樣本不足而 fallback 到全市場。

這些欄位用來判斷是否可以進入 Phase 2D，而不是直接切換正式模型。

## 18. 對策略優化的約束

請勿建議：

- 自動下單
- 券商 API 下單
- 期權策略
- 稅務優化
- 高頻交易
- 盤中即時交易
- 保證買賣訊號

可以建議：

- 更好的因子權重
- 更好的 hard filter
- 更好的 soft penalty
- 更好的缺資料處理
- sector-aware scoring
- percentile-based scoring
- market regime filter
- 回測框架
- 風險控制
- 更清楚的報告輸出

## 19. 希望 AI 回傳的格式

請用以下格式回傳：

```text
Strategy vNext Summary

1. Overall recommendation
2. Hybrid vNext
   - composite weights
   - subfactor weights
   - hard filters
   - soft penalties
3. Stop Checking Price vNext
   - composite weights
   - subfactor weights
   - hard filters
   - soft penalties
   - action logic
4. Data quality handling
5. Sector-aware adjustments
6. Backtest / validation plan
7. Implementation patch plan
8. Tests to add
9. Risks / tradeoffs
```

## 20. 一句話總結

目前程式是「美股大型 / 中大型高流動性股票」的候選排序工具；`hybrid` 偏日常量化初篩，`stop_checking_price` 偏季度品質審查。下一步最值得改善的是 sector-aware scoring、缺資料下的 confidence/penalty、估值與品質 trade-off、以及可驗證的回測框架。
