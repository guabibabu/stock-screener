#!/usr/bin/env python3
"""Local web UI for the US stock screener.

This is intentionally dependency-free: it uses Python's standard HTTP server
and the existing screener/yfinance modules. The browser stays responsive while
the local server performs slower data fetches.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import threading
import webbrowser
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from fetch_yfinance_snapshot import (  # noqa: E402
    DEFAULT_BATCH_SIZE,
    DEFAULT_RETRY_ATTEMPTS,
    DEFAULT_SNAPSHOT_PATH,
    DEFAULT_WATCHLIST_PATH,
    fetch_snapshot,
    load_watchlist,
    save_snapshot,
)
from us_stock_screener import build_report, load_records  # noqa: E402


DEFAULT_PORT = 8765
SAMPLE_UNIVERSE_PATH = SKILL_ROOT / "references" / "sample-universe.csv"


def _json_default(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _pick(row: Dict[str, Any], *keys: str) -> Any:
    lowered = {str(key).lower(): value for key, value in row.items()}
    for key in keys:
        if key in row and row[key] not in ("", None):
            return row[key]
        lowered_value = lowered.get(key.lower())
        if lowered_value not in ("", None):
            return lowered_value
    return None


def _looks_like_record(row: Dict[str, Any]) -> bool:
    return any(
        _pick(row, key) not in ("", None)
        for key in ("price", "market_cap", "avg_dollar_volume_20d", "avg_volume_20d")
    )


def _parse_csv_upload(content: str) -> Tuple[str, List[Any]]:
    rows = list(csv.reader(content.splitlines()))
    rows = [row for row in rows if any(cell.strip() for cell in row)]
    if not rows:
        return "tickers", []

    first = [cell.strip().lower() for cell in rows[0]]
    known_headers = {
        "ticker",
        "symbol",
        "code",
        "price",
        "market_cap",
        "avg_dollar_volume_20d",
        "avg_volume_20d",
    }
    has_header = bool(set(first) & known_headers)
    if not has_header:
        return "tickers", [row[0].strip().upper() for row in rows if row and row[0].strip()]

    dict_rows = list(csv.DictReader(content.splitlines()))
    dict_rows = [row for row in dict_rows if any((value or "").strip() for value in row.values())]
    if any(_looks_like_record(row) for row in dict_rows):
        return "records", dict_rows
    tickers = []
    for row in dict_rows:
        ticker = _pick(row, "ticker", "symbol", "code")
        if ticker:
            tickers.append(str(ticker).strip().upper())
    return "tickers", tickers


def _parse_json_upload(content: str) -> Tuple[str, List[Any]]:
    payload = json.loads(content)
    rows = payload.get("records") if isinstance(payload, dict) and "records" in payload else payload
    if not isinstance(rows, list):
        raise ValueError("JSON 必須是陣列，或包含 records 陣列。")
    if all(isinstance(row, str) for row in rows):
        return "tickers", [row.strip().upper() for row in rows if row.strip()]
    if all(isinstance(row, dict) for row in rows):
        dict_rows = [row for row in rows if isinstance(row, dict)]
        if any(_looks_like_record(row) for row in dict_rows):
            return "records", dict_rows
        tickers = []
        for row in dict_rows:
            ticker = _pick(row, "ticker", "symbol", "code")
            if ticker:
                tickers.append(str(ticker).strip().upper())
        return "tickers", tickers
    raise ValueError("JSON 陣列內容必須是 ticker 字串或資料物件。")


def _parse_jsonl_upload(content: str) -> Tuple[str, List[Any]]:
    rows: List[Any] = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return _parse_json_upload(json.dumps(rows))


def parse_uploaded_content(filename: str, content: str) -> Tuple[str, List[Any]]:
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".csv" or not suffix:
        return _parse_csv_upload(content)
    if suffix == ".json":
        return _parse_json_upload(content)
    if suffix in {".jsonl", ".ndjson"}:
        return _parse_jsonl_upload(content)
    raise ValueError("只支援 CSV、JSON、JSONL、NDJSON。")


def _result_to_payload(item: Any) -> Dict[str, Any]:
    return {
        "ticker": item.ticker,
        "strategy_mode": item.strategy_mode,
        "total_score": item.total_score,
        "raw_score": item.raw_score,
        "adjusted_score": item.adjusted_score,
        "penalty_score": item.penalty_score,
        "confidence_score": item.confidence_score,
        "confidence_label": item.confidence_label,
        "confidence_multiplier": item.confidence_multiplier,
        "final_score": item.final_score,
        "fundamental_score": item.fundamental_score,
        "momentum_score": item.momentum_score,
        "risk_safety_score": item.risk_safety_score,
        "factor_scores": item.factor_scores,
        "reasons": item.reasons,
        "risk_warnings": item.risk_warnings,
        "confidence_notes": item.confidence_notes,
        "penalties": item.penalties,
        "suggested_action": item.suggested_action,
        "company_snapshot": item.company_snapshot,
        "excluded_reason": item.excluded_reason,
        "exclusion_details": item.exclusion_details,
    }


def _report_to_payload(report: Any, *, source_name: str, bundle: Any = None) -> Dict[str, Any]:
    return {
        "source_name": source_name,
        "strategy_mode": report.strategy_mode,
        "review_mode": report.review_mode,
        "universe_size": report.universe_size,
        "hard_pass_count": report.hard_pass_count,
        "candidate_count": len(report.candidates),
        "excluded_count": len(report.excluded),
        "hard_excluded_count": len(report.hard_excluded),
        "soft_penalty_count": len(report.soft_penalties),
        "missing_data_count": len(report.missing_data_warnings),
        "retry_failed_count": report.retry_failed_count,
        "fetch_failed_count": report.fetch_failed_count,
        "dedupe_removed_count": report.dedupe_removed_count,
        "min_score": report.min_score,
        "effective_min_score_source": report.effective_min_score_source,
        "top_n": report.top_n,
        "dedupe_company": report.dedupe_company,
        "snapshot": None
        if bundle is None
        else {
            "as_of": bundle.as_of,
            "fetched_at": bundle.fetched_at,
            "status": bundle.status,
            "warnings": bundle.warnings[:20],
            "retry_failed_count": bundle.retry_failed_count,
            "fetch_failed_count": bundle.fetch_failed_count,
        },
        "candidates": [_result_to_payload(item) for item in report.candidates],
        "hard_excluded": [_result_to_payload(item) for item in report.hard_excluded],
        "soft_penalties": report.soft_penalties,
        "missing_data_warnings": report.missing_data_warnings,
    }


def _screen_records(
    records: Iterable[Dict[str, Any]],
    *,
    source_name: str,
    strategy_mode: str,
    force_rebalance: bool,
    min_score: Optional[float],
    bundle: Any = None,
) -> Dict[str, Any]:
    as_of = None
    if bundle is not None and getattr(bundle, "as_of", None):
        try:
            as_of = date.fromisoformat(str(bundle.as_of))
        except ValueError:
            as_of = None
    report = build_report(
        records,
        strategy_mode=strategy_mode,
        as_of=as_of,
        force_rebalance=force_rebalance,
        min_score=min_score,
    )
    return _report_to_payload(report, source_name=source_name, bundle=bundle)


def run_screen_request(payload: Dict[str, Any]) -> Dict[str, Any]:
    strategy_mode = str(payload.get("strategy_mode") or "hybrid")
    force_rebalance = bool(payload.get("force_rebalance"))
    auto_fetch = bool(payload.get("auto_fetch", True))
    source_mode = str(payload.get("source_mode") or "default_watchlist")
    min_score_raw = payload.get("min_score")
    min_score = None if min_score_raw in ("", None) else float(min_score_raw)

    if source_mode == "sample_universe":
        records = load_records(SAMPLE_UNIVERSE_PATH)
        return _screen_records(
            records,
            source_name="離線示範資料",
            strategy_mode=strategy_mode,
            force_rebalance=force_rebalance,
            min_score=min_score,
        )

    if source_mode == "uploaded":
        filename = str(payload.get("filename") or "upload.csv")
        content = str(payload.get("content") or "")
        data_kind, rows = parse_uploaded_content(filename, content)
        if data_kind == "records" and not auto_fetch:
            return _screen_records(
                rows,
                source_name=filename,
                strategy_mode=strategy_mode,
                force_rebalance=force_rebalance,
                min_score=min_score,
            )
        tickers = [str(row).strip().upper() for row in rows if str(row).strip()]
        source_name = filename
    else:
        tickers = load_watchlist(DEFAULT_WATCHLIST_PATH)
        source_name = DEFAULT_WATCHLIST_PATH.name

    if not auto_fetch:
        raise ValueError("ticker 清單需要開啟「自動抓 yfinance 資料」才可以篩選。")
    if not tickers:
        raise ValueError("找不到 ticker，請選擇有效的清單。")

    bundle = fetch_snapshot(
        tickers,
        batch_size=int(payload.get("batch_size") or DEFAULT_BATCH_SIZE),
        retry_attempts=int(payload.get("retry_attempts") or DEFAULT_RETRY_ATTEMPTS),
    )
    if bundle.status != "failed":
        save_snapshot(bundle, DEFAULT_SNAPSHOT_PATH)
    return _screen_records(
        bundle.records,
        source_name=f"yfinance snapshot ({source_name})",
        strategy_mode=strategy_mode,
        force_rebalance=force_rebalance,
        min_score=min_score,
        bundle=bundle,
    )


INDEX_HTML = r"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>美股自動選股助手 Web</title>
  <style>
    :root {
      --bg: #f5f7fb;
      --panel: #ffffff;
      --text: #111827;
      --muted: #64748b;
      --line: #dbe3ef;
      --accent: #2563eb;
      --accent-soft: #dbeafe;
      --green: #16a34a;
      --red: #dc2626;
      --amber: #d97706;
      --shadow: 0 18px 50px rgba(15, 23, 42, 0.08);
      color-scheme: light;
      font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans TC", sans-serif;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: radial-gradient(circle at 20% 0%, #e0f2fe 0, transparent 28rem), var(--bg); color: var(--text); }
    .shell { width: min(1440px, calc(100vw - 32px)); margin: 24px auto; }
    .hero { background: rgba(255,255,255,0.92); border: 1px solid var(--line); border-radius: 24px; padding: 28px; box-shadow: var(--shadow); }
    .eyebrow { color: var(--accent); font-weight: 800; letter-spacing: .08em; font-size: 12px; }
    h1 { margin: 8px 0 8px; font-size: clamp(30px, 4vw, 48px); line-height: 1.05; letter-spacing: -0.04em; }
    .sub { margin: 0; color: var(--muted); font-size: 16px; max-width: 820px; line-height: 1.7; }
    .layout { display: grid; grid-template-columns: 340px 1fr; gap: 18px; margin-top: 18px; }
    .card { background: rgba(255,255,255,0.94); border: 1px solid var(--line); border-radius: 20px; box-shadow: 0 8px 30px rgba(15, 23, 42, 0.05); }
    .controls { padding: 18px; position: sticky; top: 16px; align-self: start; }
    label { display: block; font-weight: 800; font-size: 13px; margin: 14px 0 8px; }
    select, input[type="number"], input[type="file"] { width: 100%; border: 1px solid var(--line); border-radius: 12px; padding: 11px 12px; background: white; color: var(--text); font-size: 14px; }
    .check { display: flex; gap: 10px; align-items: center; margin: 12px 0; color: var(--muted); font-weight: 700; }
    .check input { width: 18px; height: 18px; }
    .button { width: 100%; border: 0; border-radius: 14px; padding: 14px 16px; font-size: 15px; font-weight: 900; cursor: pointer; transition: transform .15s ease, box-shadow .15s ease, opacity .15s ease; }
    .button:hover { transform: translateY(-1px); }
    .button:disabled { cursor: not-allowed; opacity: .55; transform: none; }
    .primary { background: var(--accent); color: white; box-shadow: 0 12px 24px rgba(37,99,235,.22); }
    .secondary { background: #eef2f7; color: var(--text); margin-top: 10px; }
    .status { margin-top: 14px; border-radius: 16px; padding: 14px; background: var(--accent-soft); color: #1e3a8a; font-weight: 800; line-height: 1.5; }
    .spinner { display: none; width: 100%; height: 8px; margin-top: 10px; border-radius: 999px; overflow: hidden; background: #cbd5e1; }
    .spinner span { display: block; width: 38%; height: 100%; background: var(--accent); border-radius: inherit; animation: loading 1s infinite ease-in-out; }
    body.busy .spinner { display: block; }
    @keyframes loading { 0% { transform: translateX(-100%); } 100% { transform: translateX(280%); } }
    .main { padding: 18px; min-width: 0; }
    .metrics { display: grid; grid-template-columns: repeat(6, minmax(120px, 1fr)); gap: 10px; }
    .metric { border: 1px solid var(--line); border-radius: 16px; padding: 14px; background: #fff; }
    .metric .label { color: var(--muted); font-size: 12px; font-weight: 900; margin-bottom: 8px; }
    .metric .value { font-size: 24px; font-weight: 950; letter-spacing: -0.04em; }
    .section-title { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: 20px 0 10px; }
    .section-title h2 { margin: 0; font-size: 18px; }
    .pill { border-radius: 999px; background: #eef2ff; color: #3730a3; padding: 6px 10px; font-size: 12px; font-weight: 900; }
    .table-wrap { overflow: auto; border: 1px solid var(--line); border-radius: 16px; background: white; }
    table { width: 100%; border-collapse: collapse; min-width: 860px; }
    th, td { padding: 13px 14px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
    th { font-size: 12px; color: var(--muted); text-transform: uppercase; background: #f8fafc; position: sticky; top: 0; }
    tbody tr { cursor: pointer; }
    tbody tr:hover { background: #f8fbff; }
    .score { font-weight: 950; color: var(--accent); }
    .muted { color: var(--muted); }
    .detail-grid { display: grid; grid-template-columns: 1.1fr .9fr; gap: 14px; }
    .box { border: 1px solid var(--line); border-radius: 16px; background: white; padding: 16px; min-height: 160px; white-space: pre-wrap; line-height: 1.65; overflow: auto; }
    .empty { color: var(--muted); padding: 40px 16px; text-align: center; }
    .danger { color: var(--red); }
    .ok { color: var(--green); }
    .warn { color: var(--amber); }
    @media (max-width: 980px) {
      .layout { grid-template-columns: 1fr; }
      .controls { position: static; }
      .metrics { grid-template-columns: repeat(2, 1fr); }
      .detail-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div class="eyebrow">LOCAL WEB VERSION</div>
      <h1>美股自動選股助手</h1>
      <p class="sub">這版跑在瀏覽器裡，但仍使用同一套 Python 選股核心。長時間抓資料時，瀏覽器畫面會保持可互動，比桌面 Tkinter 視窗更容易看出目前狀態。</p>
    </section>

    <div class="layout">
      <aside class="card controls">
        <label>策略模式</label>
        <select id="strategy">
          <option value="hybrid">Hybrid</option>
          <option value="stop_checking_price">Stop Checking Price</option>
        </select>

        <label>資料來源</label>
        <select id="sourceMode">
          <option value="default_watchlist">使用預設 ticker 清單，自動抓 yfinance</option>
          <option value="uploaded">上傳自己的 CSV / JSON</option>
          <option value="sample_universe">離線示範資料，不抓網路</option>
        </select>

        <div id="uploadBox" style="display:none">
          <label>上傳檔案</label>
          <input id="fileInput" type="file" accept=".csv,.json,.jsonl,.ndjson" />
          <label class="check"><input id="autoFetch" type="checkbox" checked /> 自動抓 yfinance 資料</label>
        </div>

        <label class="check"><input id="forceRebalance" type="checkbox" /> 強制季度檢查</label>

        <label>自訂最低分數，可留空</label>
        <input id="minScore" type="number" min="0" max="100" step="0.1" placeholder="Hybrid 留空；Stop 預設 85" />

        <button id="runBtn" class="button primary">更新並篩選</button>
        <button id="sampleBtn" class="button secondary">只看離線示範資料</button>

        <div class="status" id="status">就緒。選好資料來源後按「更新並篩選」。</div>
        <div class="spinner"><span></span></div>
      </aside>

      <main class="card main">
        <div class="metrics">
          <div class="metric"><div class="label">輸入</div><div class="value" id="mUniverse">0</div></div>
          <div class="metric"><div class="label">候選</div><div class="value" id="mCandidates">0</div></div>
          <div class="metric"><div class="label">硬剔除</div><div class="value" id="mExcluded">0</div></div>
          <div class="metric"><div class="label">扣分</div><div class="value" id="mPenalty">0</div></div>
          <div class="metric"><div class="label">缺資料</div><div class="value" id="mMissing">0</div></div>
          <div class="metric"><div class="label">抓取失敗</div><div class="value" id="mFetchFailed">0</div></div>
        </div>

        <div class="section-title">
          <h2>候選排名</h2>
          <span class="pill" id="modePill">尚未執行</span>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>排名</th>
                <th>Ticker</th>
                <th>總分</th>
                <th>動作</th>
                <th>入選理由</th>
                <th>風險提醒</th>
              </tr>
            </thead>
            <tbody id="rows">
              <tr><td colspan="6" class="empty">尚未產生結果。</td></tr>
            </tbody>
          </table>
        </div>

        <div class="section-title"><h2>詳細內容</h2><span class="pill">點一檔股票查看</span></div>
        <div class="detail-grid">
          <div class="box" id="detail">尚未選擇股票。</div>
          <div class="box" id="exclusions">排除與資料提示會顯示在這裡。</div>
        </div>
      </main>
    </div>
  </div>

  <script>
    const $ = (id) => document.getElementById(id);
    let latest = null;
    let timer = null;
    let startedAt = 0;

    function setBusy(active, text) {
      document.body.classList.toggle('busy', active);
      $('runBtn').disabled = active;
      $('sampleBtn').disabled = active;
      if (active) {
        startedAt = Date.now();
        $('status').textContent = text || '執行中...';
        timer = setInterval(() => {
          const sec = Math.floor((Date.now() - startedAt) / 1000);
          $('status').textContent = `${text || '執行中'}，已運行 ${sec} 秒`;
        }, 1000);
      } else {
        if (timer) clearInterval(timer);
        timer = null;
        if (text) $('status').textContent = text;
      }
    }

    function sourcePayload(overrideSource) {
      const sourceMode = overrideSource || $('sourceMode').value;
      const payload = {
        strategy_mode: $('strategy').value,
        source_mode: sourceMode,
        force_rebalance: $('forceRebalance').checked,
        auto_fetch: $('autoFetch').checked,
        min_score: $('minScore').value
      };
      return payload;
    }

    async function readUploadIfNeeded(payload) {
      if (payload.source_mode !== 'uploaded') return payload;
      const file = $('fileInput').files[0];
      if (!file) throw new Error('請先選擇 CSV / JSON 檔案。');
      payload.filename = file.name;
      payload.content = await file.text();
      return payload;
    }

    async function run(overrideSource) {
      try {
        let payload = sourcePayload(overrideSource);
        payload = await readUploadIfNeeded(payload);
        setBusy(true, '正在處理');
        const res = await fetch('/api/screen', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.error || '執行失敗');
        latest = data.report;
        render(latest);
        setBusy(false, '完成。可以點候選股票查看理由。');
      } catch (err) {
        setBusy(false, `錯誤：${err.message}`);
      }
    }

    function setMetric(id, value) { $(id).textContent = value ?? 0; }

    function render(report) {
      setMetric('mUniverse', report.universe_size);
      setMetric('mCandidates', report.candidate_count);
      setMetric('mExcluded', report.hard_excluded_count);
      setMetric('mPenalty', report.soft_penalty_count);
      setMetric('mMissing', report.missing_data_count);
      setMetric('mFetchFailed', report.fetch_failed_count);
      $('modePill').textContent = `${report.strategy_mode}｜min_score ${report.min_score ?? '未設定'}｜${report.effective_min_score_source}`;

      const body = $('rows');
      body.innerHTML = '';
      if (!report.candidates.length) {
        body.innerHTML = '<tr><td colspan="6" class="empty">沒有符合條件的候選股票。</td></tr>';
      } else {
        report.candidates.forEach((item, index) => {
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td>${index + 1}</td>
            <td><strong>${item.ticker}</strong></td>
            <td class="score">${item.total_score ?? ''}</td>
            <td>${item.suggested_action || 'CANDIDATE'}</td>
            <td>${(item.reasons || []).slice(0, 2).join('；') || '等待審核'}</td>
            <td>${(item.risk_warnings || []).slice(0, 2).join('；') || '<span class="ok">無明顯風險</span>'}</td>
          `;
          tr.addEventListener('click', () => showDetail(item));
          body.appendChild(tr);
        });
        showDetail(report.candidates[0]);
      }
      renderExclusions(report);
    }

    function showDetail(item) {
      const penalties = (item.penalties || []).map(p => `- ${p.reason}：扣 ${p.points} 分`).join('\n') || '無';
      $('detail').textContent = [
        `${item.ticker}`,
        `總分：${item.total_score}`,
        `原始分：${item.raw_score}`,
        `扣分：${item.penalty_score}`,
        `信心：${item.confidence_label || 'N/A'} (${item.confidence_score ?? 'N/A'})`,
        `最終分：${item.final_score}`,
        '',
        '入選理由：',
        ...((item.reasons || []).map(r => `- ${r}`)),
        '',
        '風險提醒：',
        ...((item.risk_warnings || ['無明顯風險']).map(r => `- ${r}`)),
        '',
        '資料提醒：',
        ...((item.confidence_notes || ['無']).map(r => `- ${r}`)),
        '',
        '扣分明細：',
        penalties
      ].join('\n');
    }

    function renderExclusions(report) {
      const hard = (report.hard_excluded || []).slice(0, 80).map(item => {
        const d = (item.exclusion_details || [])[0] || {};
        return `- ${item.ticker}：${item.excluded_reason || ''}｜類別 ${d.category || 'N/A'}｜門檻 ${d.threshold ?? 'N/A'}`;
      });
      const missing = (report.missing_data_warnings || []).slice(0, 80).map(item => {
        return `- ${item.ticker}：${(item.missing_fields || []).join('、') || '部分欄位缺失'}`;
      });
      const snapshot = report.snapshot ? [
        `快照：${report.snapshot.as_of} ${report.snapshot.fetched_at}`,
        `狀態：${report.snapshot.status}`,
        ...(report.snapshot.warnings || []).slice(0, 8).map(w => `- ${w}`)
      ] : ['沒有 yfinance 快照資訊。'];
      $('exclusions').textContent = [
        '抓取 / 快照：',
        ...snapshot,
        '',
        '硬性剔除：',
        ...(hard.length ? hard : ['沒有硬性剔除。']),
        '',
        '資料缺口：',
        ...(missing.length ? missing : ['沒有資料缺口提示。'])
      ].join('\n');
    }

    $('sourceMode').addEventListener('change', () => {
      $('uploadBox').style.display = $('sourceMode').value === 'uploaded' ? 'block' : 'none';
    });
    $('runBtn').addEventListener('click', () => run());
    $('sampleBtn').addEventListener('click', () => run('sample_universe'));
  </script>
</body>
</html>
"""


class ScreenerWebHandler(BaseHTTPRequestHandler):
    server_version = "USStockScreenerWeb/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

    def _send_json(self, payload: Dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, html: str) -> None:
        data = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            self._send_html(INDEX_HTML)
            return
        if path == "/api/health":
            self._send_json({"ok": True, "service": "us-stock-screener-web"})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/api/screen":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
            raw = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw or "{}")
            report = run_screen_request(payload)
            self._send_json({"ok": True, "report": report})
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)


def serve(host: str, port: int, *, open_browser: bool = False) -> None:
    server = ThreadingHTTPServer((host, port), ScreenerWebHandler)
    url = f"http://{host}:{port}/"
    print(f"美股自動選股助手 Web 版已啟動：{url}")
    print("按 Ctrl+C 可以停止。")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止 Web 版。")
    finally:
        server.server_close()


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the local web UI for the US stock screener.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--open", action="store_true", help="Open the browser automatically.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    serve(args.host, args.port, open_browser=args.open)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
