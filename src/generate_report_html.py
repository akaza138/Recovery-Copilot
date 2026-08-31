"""Renders a batch report (as written by src/run_batch.py) into a single,
self-contained HTML file: the same metrics table the CLI prints, plus a
click-to-expand audit trail viewer per record showing the full evidence
chain (event -> diagnosis -> policy -> action -> result).

Deliberately minimal — no framework, no build step, no external assets, no
JavaScript beyond the browser's native <details> disclosure widget. This is
the whole Day-5 UI; a dashboard is explicitly out of scope for now.

Usage:
    python -m src.generate_report_html                                  # data/batch_report.json -> data/batch_report.html
    python -m src.generate_report_html --report path.json --out path.html
"""

import argparse
import html
import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_REPORT_PATH = DATA_DIR / "batch_report.json"
DEFAULT_OUT_PATH = DATA_DIR / "batch_report.html"

# Badge color classes, kept visually distinct on purpose — see the module
# docstring's "never display a simulated result styled identically to a
# confirmed one" guardrail.
ACTION_MODE_CLASS = {"real": "badge-real", "simulated": "badge-simulated"}
ACTION_RESULT_CLASS = {
    "succeeded": "badge-succeeded",
    "pending": "badge-pending",
    "failed": "badge-failed",
    "not_executed": "badge-neutral",
}
DECISION_CLASS = {
    "retry": "badge-decision-action",
    "payment_link": "badge-decision-action",
    "human_review": "badge-decision-review",
    "stand_down": "badge-decision-stand-down",
}
BAND_CLASS = {"high": "badge-band-high", "medium": "badge-band-medium", "low": "badge-band-low"}
SOURCE_LABEL = {"rule": "Rule", "llm": "LLM", "llm_fallback": "LLM fallback"}

CSS = """
:root {
  color-scheme: light;
  --bg: #f7f7f9; --panel: #ffffff; --border: #e2e2e8; --text: #1c1c24; --muted: #6b6b76;
  --green: #16794f; --green-bg: #e6f6ee;
  --amber: #92620a; --amber-bg: #fdf1de;
  --red: #a11d2b; --red-bg: #fbe9ea;
  --blue: #1856b8; --blue-bg: #e8f0fc;
  --purple: #5b3ca8; --purple-bg: #eee8fb;
  --gray-bg: #eeeef1;
}
* { box-sizing: border-box; }
body { margin: 0; padding: 2rem; background: var(--bg); color: var(--text);
  font: 15px/1.5 -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; }
.wrap { max-width: 1080px; margin: 0 auto; }
h1 { font-size: 1.5rem; margin: 0 0 0.25rem; }
.subtitle { color: var(--muted); margin: 0 0 1.5rem; }
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 1.25rem 1.5rem; margin-bottom: 1.25rem; }
.panel h2 { font-size: 1.05rem; margin: 0 0 0.75rem; }
table { width: 100%; border-collapse: collapse; }
.metrics-table td, .metrics-table th { padding: 0.4rem 0.5rem; border-bottom: 1px solid var(--border); text-align: left; }
.metrics-table td:last-child, .metrics-table th:last-child { text-align: right; font-variant-numeric: tabular-nums; }
.metrics-table tr.highlight td { font-weight: 600; }
.note { font-size: 0.85rem; color: var(--muted); background: var(--gray-bg); border-radius: 8px; padding: 0.6rem 0.8rem; margin-top: 0.5rem; white-space: pre-wrap; }
.safety-banner { border-radius: 8px; padding: 0.7rem 1rem; font-weight: 600; margin-bottom: 1.25rem; }
.safety-ok { background: var(--green-bg); color: var(--green); border: 1px solid var(--green); }
.safety-bad { background: var(--red-bg); color: var(--red); border: 1px solid var(--red); }
.stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 0.6rem; font-size: 0.9rem; }
.stat-grid div span.k { color: var(--muted); display: block; font-size: 0.8rem; }
.badge { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 999px; font-size: 0.78rem; font-weight: 600; white-space: nowrap; }
.badge-real { background: var(--blue-bg); color: var(--blue); }
.badge-simulated { background: var(--gray-bg); color: var(--muted); border: 1px dashed var(--muted); }
.badge-succeeded { background: var(--green-bg); color: var(--green); }
.badge-pending { background: var(--blue-bg); color: var(--blue); }
.badge-failed { background: var(--red-bg); color: var(--red); }
.badge-neutral { background: var(--gray-bg); color: var(--muted); }
.badge-decision-action { background: var(--blue-bg); color: var(--blue); }
.badge-decision-review { background: var(--purple-bg); color: var(--purple); }
.badge-decision-stand-down { background: var(--amber-bg); color: var(--amber); }
.badge-band-high { background: var(--green-bg); color: var(--green); }
.badge-band-medium { background: var(--amber-bg); color: var(--amber); }
.badge-band-low { background: var(--red-bg); color: var(--red); }
.demo-tag { background: #fff2c6; color: #7a5b00; border-radius: 999px; padding: 0.1rem 0.5rem; font-size: 0.75rem; font-weight: 700; }
.records { display: flex; flex-direction: column; gap: 0.5rem; }
details.record { border: 1px solid var(--border); border-radius: 8px; background: var(--panel); }
details.record summary { list-style: none; cursor: pointer; padding: 0.6rem 0.9rem; display: flex; align-items: center; gap: 0.6rem; flex-wrap: wrap; }
details.record summary::-webkit-details-marker { display: none; }
details.record summary .pid { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.82rem; color: var(--muted); }
details.record summary .amt { font-weight: 600; margin-left: auto; }
details.record[open] summary { border-bottom: 1px solid var(--border); }
.chain { padding: 0.9rem 1.1rem 1.1rem; display: flex; flex-direction: column; gap: 0.8rem; }
.chain .step { border-left: 3px solid var(--border); padding-left: 0.8rem; }
.chain .step h3 { margin: 0 0 0.3rem; font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); }
.chain .step p { margin: 0.2rem 0; }
.factors { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.8rem; background: var(--gray-bg); border-radius: 6px; padding: 0.5rem 0.7rem; margin-top: 0.3rem; }
.factors div { display: flex; justify-content: space-between; gap: 1rem; }
.factors div span:first-child { color: var(--muted); }
footer { color: var(--muted); font-size: 0.8rem; text-align: center; margin-top: 2rem; }
.cf-panel { background: var(--panel); border: 2px solid var(--text); border-radius: 10px; padding: 1.5rem 1.75rem; margin-bottom: 1.5rem; }
.cf-panel h2 { font-size: 1.2rem; margin: 0 0 0.25rem; }
.cf-panel .cf-subtitle { color: var(--muted); margin: 0 0 1rem; font-size: 0.9rem; }
.cf-table td, .cf-table th { padding: 0.5rem 0.6rem; border-bottom: 1px solid var(--border); text-align: left; }
.cf-table th:not(:first-child), .cf-table td:not(:first-child) { text-align: right; font-variant-numeric: tabular-nums; }
.cf-table tr.cf-gated td { font-weight: 700; background: var(--green-bg); }
.cf-breakdown { margin-top: 0.9rem; font-size: 0.85rem; }
.cf-breakdown h4 { margin: 0.6rem 0 0.3rem; font-size: 0.85rem; }
.cf-breakdown ul { margin: 0; padding-left: 1.2rem; }
.cf-summary { margin-top: 1.1rem; padding: 0.8rem 1rem; background: var(--gray-bg); border-radius: 8px; font-size: 0.95rem; font-weight: 600; }
"""


def _fmt_inr(paise: int) -> str:
    return f"INR {paise / 100:,.2f}"


def _fmt_pct(rate: float) -> str:
    return f"{rate * 100:.1f}%"


def _badge(label: str, cls: str) -> str:
    return f'<span class="badge {html.escape(cls)}">{html.escape(label)}</span>'


def _confirmed_recovered_note() -> str:
    return (
        "This is INR 0.00 by design, not a bug (investigated Day 4/5). Razorpay's own test-mode docs "
        "confirm there is no server-side API to complete a payment - it requires the hosted Checkout UI. "
        "A headless-browser attempt at that flow was blocked at the network layer in the build sandbox; a "
        "recheck from an unrestricted browser could not be completed either (no working browser connection "
        "available). This system's job stops at 'the customer has a working way to pay' - completing it is "
        "the customer's action, not the agent's. See README.md, 'Why Confirmed Recovered is INR 0'."
    )


def _render_metrics_table(m: dict) -> str:
    rows = [
        ("Revenue-at-risk events", str(m["revenue_at_risk_events"])),
        ("Confirmed recovered", _fmt_inr(m["confirmed_recovered_amount"])),
        ("Recovery rate", _fmt_pct(m["recovery_rate"])),
        ("Auto-recovery attempts", str(m["auto_recovery_attempts"])),
        ("Successful recoveries", str(m["successful_recoveries"])),
        ("Policy refusals (escalated)", str(m["policy_refusals_escalated"])),
        ("Unresolved", str(m["unresolved"])),
        ("Stopped by safety rules", str(m["stopped_by_safety_rules"])),
        ("Incorrect automatic actions", str(m["incorrect_automatic_actions"])),
        ("Max retry attempts allowed", str(m["max_retry_attempts_allowed"])),
    ]
    body = ""
    for label, value in rows:
        highlight = ' class="highlight"' if label == "Incorrect automatic actions" else ""
        body += f"<tr{highlight}><td>{html.escape(label)}</td><td>{html.escape(value)}</td></tr>\n"
        if label == "Confirmed recovered" and m["confirmed_recovered_amount"] == 0:
            body += f'<tr><td colspan="2"><div class="note">{html.escape(_confirmed_recovered_note())}</div></td></tr>\n'
    return f'<table class="metrics-table"><tbody>{body}</tbody></table>'


def _render_counterfactual(cf: dict) -> str:
    rows = ""
    for mode_key in ("naive", "llm_only", "gated"):
        mode = cf[mode_key]
        cls = ' class="cf-gated"' if mode_key == "gated" else ""
        rows += f"<tr{cls}><td>{html.escape(mode['label'])}</td><td>{mode['auto_actions']}</td><td>{mode['unsafe_actions']}</td></tr>\n"

    table = f"""<table class="cf-table"><thead><tr><th>Mode</th><th>Auto-actions taken</th><th>Unsafe actions</th></tr></thead>
    <tbody>{rows}</tbody></table>"""

    breakdowns = ""
    for mode_key in ("naive", "llm_only"):
        mode = cf[mode_key]
        if mode["unsafe_breakdown"]:
            items = "".join(f"<li>{count}&times; {html.escape(category)}</li>" for category, count in mode["unsafe_breakdown"].items())
            breakdowns += f'<div class="cf-breakdown"><h4>{html.escape(mode["label"])} — unsafe action breakdown</h4><ul>{items}</ul></div>'

    return f"""
    <div class="cf-panel">
      <h2>Counterfactual evaluation — what skipping the policy engine would cost</h2>
      <p class="cf-subtitle">Same {cf['total_records']} records, three decision strategies. This is the headline
        number in the project: not what Recovery Copilot does, but what it prevents.</p>
      {table}
      {breakdowns}
      <div class="cf-summary">{html.escape(cf['summary_line'])}</div>
    </div>
    """


def _render_safety_banner(m: dict) -> str:
    if m["incorrect_automatic_actions"] == 0:
        return '<div class="safety-banner safety-ok">Safety check: incorrect automatic actions = 0 (target met)</div>'
    return (
        f'<div class="safety-banner safety-bad">SAFETY VIOLATION: '
        f'{m["incorrect_automatic_actions"]} incorrect automatic action(s) — see records below</div>'
    )


def _render_ledger_status(ledger: dict) -> str:
    if ledger["total_rows"] == 0:
        return '<div class="safety-banner">Ledger: empty — nothing to verify.</div>'
    if ledger["intact"]:
        return f'<div class="safety-banner safety-ok">Ledger: intact, {ledger["rows_verified"]} rows verified (SHA-256 hash chain)</div>'
    return (
        f'<div class="safety-banner safety-bad">LEDGER TAMPERED: verified {ledger["rows_verified"]}/{ledger["total_rows"]} '
        f'rows before the chain broke at ledger_sequence={ledger["broken_at_sequence"]} '
        f'(row id={html.escape(str(ledger["broken_row_id"]))})</div>'
    )


def _render_stat_grid(m: dict) -> str:
    stats = [
        ("Revenue at risk (total)", _fmt_inr(m["revenue_at_risk_amount"])),
        ("Rule-diagnosed", str(m["rule_diagnosed_count"])),
        ("LLM-diagnosed", str(m["llm_diagnosed_count"])),
        ("LLM fallback", str(m["llm_fallback_count"])),
        ("Confidence HIGH / MEDIUM / LOW", f"{m['high_confidence_count']} / {m['medium_confidence_count']} / {m['low_confidence_count']}"),
        ("Simulated actions", str(m["simulated_action_count"])),
        ("Real test-mode actions", str(m["real_action_count"])),
        ("Pending/unconfirmed actions", str(m["pending_unconfirmed_count"])),
        ("Simulated recovery (modeled)", f"{_fmt_inr(m['simulated_recovered_amount'])} ({m['simulated_recovered_count']} events)"),
        ("Human review rate", _fmt_pct(m["human_review_rate"])),
        ("Safety-stop rate", _fmt_pct(m["safety_stop_rate"])),
        ("LLM usage / fallback rate", f"{_fmt_pct(m['llm_usage_rate'])} / {_fmt_pct(m['llm_fallback_rate'])}"),
    ]
    cells = "".join(f'<div><span class="k">{html.escape(k)}</span>{html.escape(v)}</div>' for k, v in stats)
    return f'<div class="stat-grid">{cells}</div>'


def _factor_rows(factors: dict) -> str:
    rows = "".join(f"<div><span>{html.escape(str(k))}</span><span>{html.escape(str(v))}</span></div>" for k, v in factors.items())
    return f'<div class="factors">{rows}</div>'


def _render_record(row: dict) -> str:
    pid = html.escape(row["external_payment_id"])
    demo_tag = f'<span class="demo-tag">{html.escape(row["canonical_demo_case"].replace("_", " ").upper())}</span> ' if row.get("canonical_demo_case") else ""
    band_badge = _badge(row["confidence_band"].upper(), BAND_CLASS.get(row["confidence_band"], "badge-neutral"))
    decision_badge = _badge(row["decision_action"].replace("_", " ").upper(), DECISION_CLASS.get(row["decision_action"], "badge-neutral"))
    mode_badge = _badge(row["action_mode"].upper(), ACTION_MODE_CLASS.get(row["action_mode"], "badge-neutral"))
    result_badge = _badge(row["action_result"].replace("_", " ").upper(), ACTION_RESULT_CLASS.get(row["action_result"], "badge-neutral"))

    summary = (
        f'<summary>{demo_tag}<span class="pid">{pid}</span>'
        f'{decision_badge}{band_badge}{mode_badge}{result_badge}'
        f'<span class="amt">{html.escape(_fmt_inr(row["amount"]))}</span></summary>'
    )

    model_conf = f"{row['model_reported_confidence']:.2f} (raw, LLM-reported — never read by the policy engine)" if row.get("model_reported_confidence") is not None else "n/a (rule-based; no raw model confidence exists)"

    chain = f"""
    <div class="chain">
      <div class="step">
        <h3>Event</h3>
        <p><strong>{html.escape(row.get('failure_code') or '')}</strong> / {html.escape(row.get('failure_reason') or '')}</p>
        <p>{html.escape(row.get('failure_description') or '')}</p>
      </div>
      <div class="step">
        <h3>Diagnosis</h3>
        <p>root_cause: <strong>{html.escape(row['diagnosis_root_cause'])}</strong> &middot; source: {html.escape(SOURCE_LABEL.get(row['diagnosis_source'], row['diagnosis_source']))} &middot; band: {band_badge}</p>
        <p>model_reported_confidence: {html.escape(model_conf)}</p>
        <p>{html.escape(row.get('diagnosis_evidence') or '')}</p>
      </div>
      <div class="step">
        <h3>Policy decision</h3>
        <p>{decision_badge} &middot; reason: <code>{html.escape(row['decision_reason'])}</code></p>
        {_factor_rows(row.get('decision_factors') or {})}
      </div>
      <div class="step">
        <h3>Action</h3>
        <p>{mode_badge} {result_badge}{' &middot; ref: ' + html.escape(row['razorpay_reference']) if row.get('razorpay_reference') else ''}</p>
        <p>{html.escape(row.get('action_evidence') or '')}</p>
      </div>
      <div class="step">
        <h3>Observed outcome</h3>
        <p>failed_payment.status: <strong>{html.escape(row.get('failed_payment_status') or '')}</strong>{' &middot; stop_reason: ' + html.escape(row['stop_reason']) if row.get('stop_reason') else ''}</p>
      </div>
    </div>
    """

    return f'<details class="record">{summary}{chain}</details>'


def render_html(report: dict) -> str:
    m = report["metrics"]
    mode_label = "REAL (--execute-real)" if report.get("execute_real") else "SIMULATED (default)"
    records_html = "\n".join(_render_record(r) for r in report["records"])

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Recovery Copilot — Batch Report</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <h1>Recovery Copilot — Batch Report</h1>
  <p class="subtitle">An AI-assisted revenue recovery engine, not an autonomous agent: rules and an LLM
    only ever propose a diagnosis; a deterministic policy engine decides every action.
    Mode: <strong>{html.escape(mode_label)}</strong> &middot; {m['total_records']} records processed.</p>

  {_render_counterfactual(report["counterfactual"]) if report.get("counterfactual") else ""}

  {_render_safety_banner(m)}
  {_render_ledger_status(report["ledger"]) if report.get("ledger") else ""}

  <div class="panel">
    <h2>Batch metrics</h2>
    {_render_metrics_table(m)}
  </div>

  <div class="panel">
    <h2>Also reported</h2>
    {_render_stat_grid(m)}
  </div>

  <div class="panel">
    <h2>Audit trail — every record, click to expand</h2>
    <div class="records">
      {records_html}
    </div>
  </div>

  <footer>Generated by src/generate_report_html.py from data/batch_report.json. Static file, no server, no external assets.</footer>
</div>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT_PATH)
    args = parser.parse_args()

    report = json.loads(args.report.read_text(encoding="utf-8"))
    html_out = render_html(report)
    args.out.write_text(html_out, encoding="utf-8")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
