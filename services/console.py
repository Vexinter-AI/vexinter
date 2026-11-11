"""Vexinter admin console (§13).

Pages:    /console (dashboard), /console/tasks (§13.1),
          /console/tasks/{id} (explorer, §13.2), /console/cases (§13.3),
          /console/agents (§13.4), /console/metrics (§13.5).

Authentication: login with the admin token → HMAC-signed cookie
(HttpOnly, SameSite=Lax). With dev_mode or no token, it stays open.

The console operates on the IN-PROCESS runtime: it does not make HTTP
calls to itself and does not duplicate the data plane's authorization.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import DictLoader, Environment, select_autoescape

from vexinter.core.events import EventType
from vexinter.core.graph import execution_graph
from vexinter.core.metrics import compute_metrics
from vexinter.core.recovery import StateError
from vexinter.core.statemachine import (
    VALID,
    ExecutionState,
    derive_execution_state,
)
from vexinter.core.store import EventQuery

SESSION_TTL_S = 8 * 3600
COOKIE = "vex_console"

def _q(texto) -> str:
    """Message for a redirect URL: URL-encoded, not HTML-encoded."""
    return quote(str(texto), safe="")


class LoginLimiter:
    """Rate limit for failed access attempts, per origin.

    After `max_fails` failures within `window_s` seconds, that origin is
    locked for `lock_s` seconds (even with the correct token). It is per
    origin and in memory: behind a reverse proxy all accesses share the
    proxy's address, and a restart clears it.
    """

    def __init__(self, max_fails: int = 5, window_s: float = 900.0,
                 lock_s: float = 900.0, clock=time.monotonic) -> None:
        self._max, self._window, self._lock_s = max_fails, window_s, lock_s
        self._clock = clock
        self._fails: dict[str, list[float]] = {}
        self._locked: dict[str, float] = {}

    def blocked(self, key: str) -> float:
        """Seconds remaining before that origin can try again."""
        resto = self._locked.get(key, 0.0) - self._clock()
        if resto <= 0:
            self._locked.pop(key, None)
            return 0.0
        return resto

    def failure(self, key: str) -> None:
        t = self._clock()
        vivos = [x for x in self._fails.get(key, []) if t - x < self._window]
        vivos.append(t)
        self._fails[key] = vivos
        if len(vivos) >= self._max:
            self._locked[key] = t + self._lock_s
            self._fails.pop(key, None)
        if len(self._fails) > 10_000:                # don't grow unboundedly
            self._fails = {k: v for k, v in self._fails.items()
                           if v and t - v[-1] < self._window}

    def success(self, key: str) -> None:
        self._fails.pop(key, None)
        self._locked.pop(key, None)

def _sign(token: str, name: str, exp: int) -> str:
    return hmac.new(token.encode(), f"{name}.{exp}".encode(),
                    hashlib.sha256).hexdigest()

def _issue(token: str, name: str) -> str:
    """Session cookie: name.expiry.signature, signed with THAT
    operator's token; it cannot be presented as someone else's."""
    exp = int(time.time()) + SESSION_TTL_S
    return f"{name}.{exp}.{_sign(token, name, exp)}"

def _session(tokens_by_name: dict[str, str], value: str | None) -> str | None:
    """Operator name of a valid cookie, or None."""
    if not value:
        return None
    partes = value.split(".")
    if len(partes) != 3:
        return None
    name, exp, sig = partes
    token = tokens_by_name.get(name)
    if token is None or not exp.isdigit() or int(exp) < time.time():
        return None
    if not hmac.compare_digest(_sign(token, name, int(exp)), sig):
        return None
    return name

def preview_case(rt, case_id: str) -> dict | None:
    """Queries the CURRENT state of a pending case, without executing anything.

    No verdict is recorded (record=False): looking at the queue does not
    generate spurious evidence; the recording re-verification is done by decide().
    """
    case = rt.human_queue.get_case(case_id)
    if case is None:
        return None
    run = rt.runs.get(case.task_id)
    if run is None:
        return {"case_id": case_id, "status_now": "unknown",
                "would_skip_execution": False,
                "detail": "the task is no longer in memory (restart pending)"}
    v = rt.verifier.verify(run, record=False)
    return {"case_id": case_id, "case_status": case.status.value,
            "status_now": v.status,
            "provisional": v.provisional,
            "would_skip_execution": v.status == "met",
            "detail": v.detail}

_STATE_COLOR = {
    "in_progress": "#3b82f6", "pending_verification": "#6366f1",
    "provisionally_satisfied": "#eab308", "satisfied": "#16a34a",
    "failed": "#dc2626", "inconclusive": "#f97316",
    "pending_recovery": "#d97706",
    "pending_human_decision": "#b91c1c",
}
_VERDICT_BADGE = {"met": "ok", "not_met": "bad", "inconclusive": "warn"}
_RISK_BADGE = {"bajo": "ok", "medio": "warn", "alto": "bad"}

def _esc(s) -> str:
    return html.escape(str(s if s is not None else ""))

def _short(value: str, n: int = 12) -> str:
    return value if len(value) <= n else value[:n] + "…"

def _node_class(t: str) -> str:
    if t.startswith(("task.", "execution.")): return "n-life"
    if t.startswith("tool."):                 return "n-tool"
    if t.startswith("verifier."):             return "n-verify"
    if t.startswith(("recovery.", "correction.")): return "n-recovery"
    if t.startswith("message."):               return "n-msg"
    if t.startswith("identity."):             return "n-ident"
    if t.startswith(("memory.", "followup.")): return "n-mem"
    return "n-life"

_EDGE_CLASS = {"se_origino_en": "e-caused", "depende_de": "e-dep",
               "fue_verificado_por": "e-ver", "compenso_a": "e-comp"}

def svg_graph(graph: dict) -> str:
    nodes, edges = graph.get("nodes", []), graph.get("edges", [])
    if not nodes:
        return "<p class='muted'>No recorded events.</p>"
    fwd = [e for e in edges if e["rel"] != "compenso_a"]
    back = [e for e in edges if e["rel"] == "compenso_a"]

    preds: dict[str, list[str]] = defaultdict(list)
    for e in fwd:
        preds[e["target"]].append(e["source"])
    rank: dict[str, int] = {}
    def _rank(nid: str, seen: frozenset) -> int:
        if nid in rank:
            return rank[nid]
        if nid in seen:
            return 0
        ps = preds.get(nid, [])
        rank[nid] = 0 if not ps else max(_rank(p, seen | {nid}) for p in ps) + 1
        return rank[nid]
    for n in nodes:
        _rank(n["id"], frozenset())

    by_rank: dict[int, list] = defaultdict(list)
    for n in nodes:
        by_rank[rank[n["id"]]].append(n)
    for items in by_rank.values():
        items.sort(key=lambda n: n.get("ts", ""))

    W, H, COL_W, ROW_H, X0, Y0 = 200, 52, 244, 104, 12, 12
    pos: dict[str, tuple[int, int]] = {}
    for r, items in by_rank.items():
        for i, n in enumerate(items):
            pos[n["id"]] = (X0 + i * COL_W, Y0 + r * ROW_H)
    width = X0 * 2 + max(len(v) for v in by_rank.values()) * COL_W - (COL_W - W)
    height = Y0 * 2 + (max(by_rank) + 1) * ROW_H - (ROW_H - H)

    parts = [
        (f"<svg class='graph' viewBox='0 0 {width} {height}' "
        f"width='100%' role='img' aria-label='execution graph'>"),
        ("<defs><marker id='arr' viewBox='0 0 10 10' refX='9' refY='5' "
        "markerWidth='7' markerHeight='7' orient='auto-start-reverse'>"
        "<path d='M0,0 L10,5 L0,10 z' fill='#64748b'/></marker></defs>"),
        ("<style>"
        ".n-life rect{fill:#334155}.n-tool rect{fill:#0f766e}"
        ".n-verify rect{fill:#15803d}.n-recovery rect{fill:#b45309}"
        ".n-msg rect{fill:#6d28d9}.n-ident rect{fill:#475569}"
        ".n-mem rect{fill:#7e22ce}"
        "g.node text{fill:#e2e8f0;font:600 11px system-ui}"
        "g.node text.sub{fill:#94a3b8;font:400 9px system-ui}"
        ".e-caused{stroke:#64748b}.e-dep{stroke:#0ea5e9;stroke-dasharray:3 3}"
        ".e-ver{stroke:#22c55e}.e-comp{stroke:#ef4444;stroke-dasharray:6 4}"
        "path.edge{fill:none;stroke-width:1.6}"
        "</style>"),
    ]
    for e in fwd:
        if e["source"] not in pos or e["target"] not in pos:
            continue
        x1, y1 = pos[e["source"]]; x2, y2 = pos[e["target"]]
        parts.append(
            f"<path class='edge {_EDGE_CLASS.get(e['rel'], 'e-caused')}' "
            f"d='M{x1 + W},{y1 + H / 2} C{x1 + W + 40},{y1 + H / 2} "
            f"{x2 - 40},{y2 + H / 2} {x2},{y2 + H / 2}' marker-end='url(#arr)'>"
            f"<title>{_esc(e['rel'])}</title></path>")
    for e in back:
        if e["source"] not in pos or e["target"] not in pos:
            continue
        x1, y1 = pos[e["source"]]; x2, y2 = pos[e["target"]]
        parts.append(
            f"<path class='edge e-comp' d='M{x1 + W / 2},{y1} "
            f"C{x1 + W / 2},{y1 - 46} {x2 + W / 2},{y2 + H + 46} "
            f"{x2 + W / 2},{y2 + H}' marker-end='url(#arr)'>"
            f"<title>compenso_a</title></path>")
    for n in nodes:
        x, y = pos[n["id"]]
        label = _esc(n.get("label", "")[:30])
        sub = _esc(f"{n.get('ts', '')[11:19]} · {_short(n['id'])}")
        tip = _esc(json.dumps({k: n.get(k) for k in ("type", "ts", "id")},
                              ensure_ascii=False))
        parts.append(
            f"<g class='node {_node_class(n['type'])}'><title>{tip}</title>"
            f"<rect x='{x}' y='{y}' width='{W}' height='{H}' rx='9'/>"
            f"<text x='{x + 10}' y='{y + 21}'>{label}</text>"
            f"<text class='sub' x='{x + 10}' y='{y + 38}'>{sub}</text></g>")
    parts.append("</svg>")
    return "".join(parts)

_T: dict[str, str] = {}

_T["base.html"] = '''<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vexinter — {% block title %}console{% endblock %}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{
 --bg:#0a0e16;--panel:#111a2b;--panel-2:#0c1320;--line:rgba(148,163,184,.14);--line-2:rgba(148,163,184,.07);
 --fg:#e6edf6;--muted:#8b98ac;--faint:#5c6b80;
 --accent:#38bdf8;--ok:#34d399;--bad:#f87171;--warn:#fbbf24;--info:#93c5fd;
 --radius:12px;--radius-s:8px;
}
*{box-sizing:border-box}
body{margin:0;color:var(--fg);font:14px/1.55 Inter,system-ui,sans-serif;-webkit-font-smoothing:antialiased;
 background:var(--bg);
 background-image:radial-gradient(1100px 480px at 85% -10%,rgba(56,189,248,.08),transparent 60%),
                  radial-gradient(900px 420px at -10% 110%,rgba(99,102,241,.06),transparent 60%)}
.app{display:grid;grid-template-columns:232px minmax(0,1fr);min-height:100vh}
aside{position:sticky;top:0;height:100vh;padding:18px 12px;display:flex;flex-direction:column;
 background:rgba(12,19,32,.72);border-right:1px solid var(--line-2);backdrop-filter:blur(10px)}
.logo{display:flex;align-items:center;gap:10px;padding:4px 8px 16px}
.logo .mark{width:32px;height:32px;border-radius:10px;display:grid;place-items:center;flex:none;
 background:linear-gradient(135deg,#0ea5e9,#6366f1);color:#051220;font-weight:800;font-size:15px}
.logo b{font-size:15px;letter-spacing:.4px}
.logo small{display:block;color:var(--muted);font-size:10px;font-weight:500;margin-top:1px}
nav{display:flex;flex-direction:column;gap:2px}
nav a{display:flex;align-items:center;gap:11px;padding:9px 10px;border-radius:var(--radius-s);
 color:var(--muted);text-decoration:none;font-weight:500;font-size:13.5px;transition:all .15s}
nav a svg{width:17px;height:17px;stroke-width:1.8;flex:none;opacity:.85}
nav a:hover{color:var(--fg);background:rgba(148,163,184,.08)}
nav a.on{color:#e0f2fe;background:linear-gradient(90deg,rgba(14,165,233,.18),rgba(14,165,233,.04));
 box-shadow:inset 2px 0 0 var(--accent)}
.side-foot{margin-top:auto;padding:8px 10px;color:var(--faint);font-size:11px;display:flex;flex-direction:column;gap:6px}
.live{display:inline-flex;align-items:center;gap:7px}
.live .dot{width:7px;height:7px;border-radius:50%;background:var(--ok);animation:pulse 2s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(52,211,153,.45)}70%{box-shadow:0 0 0 7px rgba(52,211,153,0)}100%{box-shadow:0 0 0 0 rgba(52,211,153,0)}}
main{padding:26px 32px;max-width:1460px;min-width:0}
h1{font-size:20px;font-weight:700;margin:0 0 4px;letter-spacing:-.2px}
h3{font-size:14px;margin:0 0 8px;word-break:break-word}
.muted{color:var(--muted);font-size:12.5px}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px;margin:16px 0}
.card{background:var(--panel);border:1px solid var(--line-2);border-radius:var(--radius);padding:15px 16px;transition:border-color .15s}
.card:hover{border-color:var(--line)}
.kpi{position:relative}
.kpi .num{font-size:27px;font-weight:700;letter-spacing:-.5px;margin-top:6px}
.kpi .lbl{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.5px;margin-top:3px}
.kpi .ico{position:absolute;top:14px;right:14px;color:var(--faint)}
.kpi .ico svg{width:17px;height:17px;stroke-width:1.7}
.panel{background:var(--panel);border:1px solid var(--line-2);border-radius:var(--radius);padding:16px 18px;margin:14px 0}
.panel-head{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
table{border-collapse:collapse;width:100%;margin:12px 0 4px;font-size:13px}
th,td{padding:8px 12px;text-align:left;border-bottom:1px solid var(--line-2);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:10.5px;text-transform:uppercase;letter-spacing:.6px}
tr:hover td{background:rgba(148,163,184,.04)}
.badge{display:inline-block;padding:2px 10px;border-radius:99px;font-size:11px;font-weight:600;letter-spacing:.2px}
.b-ok{background:rgba(52,211,153,.14);color:#6ee7b7}
.b-bad{background:rgba(248,113,113,.14);color:#fca5a5}
.b-warn{background:rgba(251,191,36,.14);color:#fde68a}
.b-info{background:rgba(147,197,253,.14);color:#bfdbfe}
.flash{position:fixed;right:22px;bottom:22px;z-index:50;padding:11px 16px;border-radius:var(--radius-s);
 background:rgba(52,211,153,.14);border:1px solid rgba(52,211,153,.35);color:#bbf7d0;
 font-size:13px;max-width:420px;box-shadow:0 8px 30px rgba(0,0,0,.45);animation:up .25s ease;cursor:pointer}
.flash.err{background:rgba(248,113,113,.14);border-color:rgba(248,113,113,.35);color:#fecaca}
.flash.hide{opacity:0;transition:opacity .4s}
@keyframes up{from{transform:translateY(8px);opacity:0}}
.okbox{background:rgba(52,211,153,.1);color:#a7f3d0;border:1px solid rgba(52,211,153,.25);border-radius:var(--radius-s);padding:10px 14px;margin:12px 0;font-size:13px}
.warnbox{background:rgba(251,191,36,.09);color:#fde68a;border:1px solid rgba(251,191,36,.25);border-radius:var(--radius-s);padding:10px 14px;margin:12px 0;font-size:13px}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}
.chip{padding:4px 13px;border-radius:99px;font-size:12px;background:var(--panel-2);color:var(--muted);
 text-decoration:none;border:1px solid var(--line-2);transition:all .15s}
.chip:hover{color:var(--fg);border-color:var(--line)}
.chip.on{background:linear-gradient(90deg,#0ea5e9,#38bdf8);color:#041220;font-weight:700;border-color:transparent}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
form.inline{display:inline}
button,.btn{background:#1c2740;color:var(--fg);border:1px solid var(--line);border-radius:var(--radius-s);
 padding:8px 15px;font:600 13px Inter,sans-serif;cursor:pointer;transition:all .15s}
button:hover{background:#26334f;border-color:rgba(148,163,184,.3)}
button.danger{background:rgba(248,113,113,.12);border-color:rgba(248,113,113,.3);color:#fca5a5}
button.danger:hover{background:rgba(248,113,113,.2)}
input,select{background:var(--panel-2);color:var(--fg);border:1px solid var(--line);border-radius:var(--radius-s);
 padding:8px 11px;font:13px Inter,sans-serif;width:100%}
input:focus{outline:none;border-color:var(--accent)}
label{display:block;font-size:11px;color:var(--muted);margin:11px 0 4px;text-transform:uppercase;letter-spacing:.4px}
pre{background:var(--panel-2);border:1px solid var(--line-2);padding:10px 12px;border-radius:var(--radius-s);
 overflow:auto;font-size:12px;font-family:ui-monospace,Menlo,monospace;color:#c9d5e5;white-space:pre-wrap;word-break:break-all}
details summary{cursor:pointer;color:var(--muted);font-size:12px}
details summary:hover{color:var(--fg)}
.legend{display:flex;gap:14px;flex-wrap:wrap;margin:8px 0;font-size:11px;color:var(--muted)}
.sw{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.split{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:12px;margin-top:10px}
#graph{height:560px;border:1px solid var(--line-2);border-radius:var(--radius-s);background:var(--panel-2)}
.node-info{background:var(--panel-2);border:1px solid var(--line-2);border-radius:var(--radius-s);
 padding:14px;overflow:auto;max-height:560px}
@media(max-width:960px){.app{grid-template-columns:1fr}aside{position:static;height:auto;flex-direction:row;flex-wrap:wrap}.split,.grid2{grid-template-columns:1fr}}
</style></head><body>
{% set ic = {
 'panel':'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>',
 'exec':'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="m12 2 9 5-9 5-9-5 9-5z"/><path d="m3 12 9 5 9-5"/><path d="m3 17 9 5 9-5"/></svg>',
 'inbox':'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>',
 'users':'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
 'chart':'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="20" x2="12" y2="10"/><line x1="18" y1="20" x2="18" y2="4"/><line x1="6" y1="20" x2="6" y2="16"/></svg>',
 'shield':'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"><path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/></svg>' } %}
<div class="app">
<aside>
  <div class="logo"><div class="mark">V</div><div><b>VEXINTER</b><small>agent reliability</small></div></div>
  <nav>
    <a href="/console">{{ ic.panel|safe }}<span>Panel</span></a>
    <a href="/console/tasks">{{ ic.exec|safe }}<span>Executions</span></a>
    <a href="/console/cases">{{ ic.inbox|safe }}<span>Decisions</span></a>
    <a href="/console/agents">{{ ic.users|safe }}<span>Agents</span></a>
    <a href="/console/metrics">{{ ic.chart|safe }}<span>Metrics</span></a>
  </nav>
  <div class="side-foot">
    <span class="live"><span class="dot"></span>live console</span>
    <span>v0.5 · verifiable ledger</span>
  </div>
</aside>
<main>
{% if msg %}<div class="flash">{{ msg }}</div>{% endif %}
{% block content %}{% endblock %}
</main>
</div>
<script>
const _f=document.querySelector('.flash');
if(_f){setTimeout(()=>_f.classList.add('hide'),4500);_f.onclick=()=>_f.remove();}
document.querySelectorAll('nav a').forEach(a=>{const h=a.getAttribute('href');
 if(location.pathname===h||(h!=='/console'&&location.pathname.startsWith(h+'/')))a.classList.add('on');});
</script>
</body></html>'''

_T["login.html"] = """{% extends "base.html" %}{% block title %}access{% endblock %}
{% block content %}<div style="max-width:380px;margin:80px auto">
<h1>Admin console</h1>
<p class="muted">The admin token is required.</p>
<div class="panel">
<form method="post" action="/console/login">
<label>Token</label><input type="password" name="token" autofocus>
<button style="margin-top:12px;width:100%">Sign in</button></form>
{% if err %}<div class="flash err" style="margin-top:12px">{{ err }}</div>{% endif %}
</div></div>{% endblock %}"""

_T["dashboard.html"] = '''{% extends "base.html" %}{% block title %}panel{% endblock %}
{% block content %}
<h1>Panel</h1>
<p class="muted">Evidence, not claims: everything you see comes from the ledger.</p>
<div class="cards">
  <div class="card kpi"><div class="ico">{{ ic.panel|safe }}</div><div class="num" id="kpi-ejec">{{ metrics['executions'] }}</div><div class="lbl">Executions</div></div>
  <div class="card kpi"><div class="ico">{{ ic.shield|safe }}</div><div class="num" id="kpi-falsos">{{ metrics['false_successes_detected'] }}</div><div class="lbl">Detected false successes</div></div>
  <div class="card kpi"><div class="ico">{{ ic.exec|safe }}</div><div class="num" id="kpi-disc">{{ metrics['discrepancies'] }}</div><div class="lbl">Discrepancies</div></div>
  <div class="card kpi"><div class="ico">{{ ic.inbox|safe }}</div><div class="num" id="kpi-rec">{{ metrics['retries_or_compensations'] }}</div><div class="lbl">Retries / compensations</div></div>
  <div class="card kpi"><div class="ico">{{ ic.users|safe }}</div><div class="num" id="kpi-dec">{{ metrics['human_decisions_requested'] }}</div><div class="lbl">Human decisions</div></div>
  <div class="card kpi"><div class="ico">{{ ic.chart|safe }}</div><div class="num" id="kpi-cases">{{ n_cases }}</div><div class="lbl">Pending cases</div></div>
</div>
<div class="grid2">
  <div class="panel"><div class="panel-head"><b>Final states</b><span class="muted">refreshes every 3 s</span></div>
    <div id="donut" style="height:320px"></div></div>
  <div class="panel"><div class="panel-head"><b>Latest executions</b>
    <a class="chip" href="/console/tasks">view all</a></div>
    <table><tr><th>Task</th><th>Objective</th><th>State</th></tr>
    {% for t in tasks %}<tr>
      <td class="mono"><a href="/console/tasks/{{ t.task_id }}" style="color:var(--accent);text-decoration:none">{{ t.task_id[:14] }}…</a></td>
      <td>{{ t.objective[:46] }}</td>
      <td><span class="badge {{ t.badge }}">{{ t.state_es }}</span></td></tr>
    {% else %}<tr><td colspan="3" class="muted">no tasks</td></tr>{% endfor %}</table>
  </div>
</div>
<script src="/console-assets/echarts.min.js"></script>
<script>
(function(){
 const C={satisfied:'#34d399',failed:'#f87171',inconclusive:'#fb923c',provisionally_satisfied:'#eab308',
  in_progress:'#3b82f6',pending_verification:'#6366f1',pending_recovery:'#f59e0b',
  pending_human_decision:'#fb7185'};
 let donut=null;
 function render(E){
  if(!window.echarts)return;
  const el=document.getElementById('donut'); if(!el)return;
  if(!donut)donut=echarts.init(el);
  const total=Object.values(E).reduce((a,b)=>a+b,0);
  donut.setOption({backgroundColor:'transparent',tooltip:{trigger:'item'},
   legend:{bottom:0,textStyle:{color:'#8b98ac',fontSize:11},itemWidth:10,itemHeight:10,icon:'circle'},
   graphic:[{type:'text',left:'center',top:'41%',style:{text:String(total),fill:'#e6edf6',fontSize:28,fontWeight:700}}],
   series:[{type:'pie',radius:['58%','82%'],center:['50%','44%'],label:{show:false},
    itemStyle:{borderColor:'#111a2b',borderWidth:2},
    data:Object.entries(E).map(([k,v])=>({name:k,value:v,itemStyle:{color:C[k]||'#64748b'}}))}]});
 }
 const set=(id,v)=>{const e=document.getElementById(id);if(e)e.textContent=v;};
 render({{ estados|tojson }});
 async function tick(){try{
  const j=await(await fetch('/console/api/summary')).json();const m=j.metrics;
  set('kpi-ejec',m.executions);set('kpi-falsos',m.false_successes_detected);
  set('kpi-disc',m.discrepancies);set('kpi-rec',m.retries_or_compensations);
  set('kpi-dec',m.human_decisions_requested);set('kpi-cases',j.cases);
  render(m.final_states||{});
 }catch(e){}}
 setInterval(tick,3000);
})();
</script>
{% endblock %}'''

_T["tasks.html"] = """{% extends "base.html" %}{% block title %}executions{% endblock %}
{% block content %}<h1>Executions</h1>
<p class="muted">States per the formal state machine (§13.1): an
unlisted transition is structurally impossible.</p>
<div class="chips"><a class="chip {{ 'on' if not state else '' }}"
href="/console/tasks">all</a>
{% for s in states %}<a class="chip {{ 'on' if state == s else '' }}"
href="/console/tasks?state={{ s }}">{{ s }}</a>{% endfor %}</div>
<table><tr><th>Task</th><th>Agent</th><th>Objective</th><th>State</th></tr>
{% for t in tasks %}
<tr><td><a href="/console/tasks/{{ t.task_id }}" style="color:#38bdf8">
{{ t.task_id[:16] }}…</a></td><td>{{ t.agent or "—" }}</td>
<td>{{ t.objective[:52] }}</td>
<td><span class="badge {{ t.badge }}">{{ t.state_es }}</span></td></tr>
{% else %}<tr><td colspan="4" class="muted">no executions
{% if state %}in this state{% endif %}</td></tr>{% endfor %}</table>
<details class="panel"><summary>State machine: valid transitions</summary>
<table><tr><th>State</th><th>Can transition to</th></tr>
{% for s, targets in machine %}
<tr><td><span class="badge {{ s.badge }}">{{ s.value }}</span></td>
<td class="muted">{{ targets }}</td></tr>{% endfor %}</table></details>
{% endblock %}"""

_T["task_detail.html"] = '''{% extends "base.html" %}{% block title %}execution{% endblock %}
{% block content %}
<h1>Execution <span class="muted mono">{{ task_id }}</span></h1>
<p class="muted">{{ objective }} · agent <b>{{ agent or "—" }}</b></p>
<p><span class="badge {{ badge_class }}" style="font-size:13px;padding:4px 14px">{{ state_es }}</span></p>
{% if insufficient %}
<div class="warnbox">⚠ <b>Insufficient evidence.</b> The <b>inconclusive</b> result must not be treated as success or as definitive failure: a review is scheduled or pending (§4.3).</div>{% endif %}
{% if audit.ok %}
<div class="okbox">Intact chain: {{ audit.events_checked }} events cryptographically verified.</div>
{% else %}<div class="flash err" style="position:static;margin:12px 0;max-width:none"><b>ALTERED chain:</b> {{ audit.problems | join(" · ") }}</div>{% endif %}
<div class="panel"><b>Verdicts</b>
{% if verdicts %}<table><tr><th>Time</th><th>State</th><th>Scope</th><th>Detail</th></tr>
{% for v in verdicts %}<tr><td class="muted">{{ v.ts }}</td>
<td><span class="badge {{ v.cls }}">{{ v.status }}</span>{% if v.provisional %} <span class="badge b-warn">provisional</span>{% endif %}</td>
<td class="muted">{{ v.scope }}</td><td class="muted">{{ v.detail or "—" }}</td></tr>{% endfor %}</table>
{% else %}<p class="muted">No verdicts recorded.</p>{% endif %}</div>
<div class="panel"><div class="panel-head"><b>Event graph</b>
<div class="legend">
<span><span class="sw" style="background:#334155"></span>lifecycle</span>
<span><span class="sw" style="background:#0f766e"></span>tools</span>
<span><span class="sw" style="background:#15803d"></span>verification</span>
<span><span class="sw" style="background:#b45309"></span>recovery</span>
<span><span class="sw" style="background:#6d28d9"></span>messages</span>
<span><span class="sw" style="background:#7e22ce"></span>memory</span>
<span style="color:#38bdf8">┄ depends on</span>
<span style="color:#f87171">┄┄ compenso_a (to the past)</span>
</div></div>
<div class="split"><div id="graph"></div>
<aside class="node-info" id="node-info"><h3>Node detail</h3>
<p class="muted">Click a node to inspect its evidence and relations. Drag to pan; scroll to zoom.</p></aside></div>
</div>
<div class="panel"><b>Timeline</b>
<table><tr><th>#</th><th>Time</th><th>Type</th><th>Detail</th><th>Relations</th><th>Payload</th></tr>
{% for e in events %}
<tr><td class="muted">{{ e.seq }}</td><td class="muted">{{ e.ts }}</td>
<td><span class="badge b-info">{{ e.type }}</span></td><td>{{ e.label }}</td>
<td class="muted">{{ e.rels }}</td>
<td>{% if e.payload != '{}' %}<details><summary>view</summary><pre>{{ e.payload }}</pre></details>{% else %}<span class="muted">—</span>{% endif %}</td></tr>
{% endfor %}</table></div>
<script src="/console-assets/cytoscape.min.js"></script>
<script src="/console-assets/dagre.min.js"></script>
<script src="/console-assets/cytoscape-dagre.js"></script>
<script>
(function(){
 const wrap=document.getElementById('graph');
 if(!window.cytoscape||!window.dagre||!window.cytoscapeDagre){
  wrap.innerHTML='<p class="muted" style="padding:16px">Could not load the graph library (offline?). The rest of the page works normally.</p>';
  return;}
 if(!cytoscape.hasExtension||!cytoscape.hasExtension('dagre')){
  try{cytoscape.use(window.cytoscapeDagre);}catch(_){}}
 const G={{ graph|tojson }};
 const CLS=t=>t.indexOf('tool.')===0?'n-tool':t.indexOf('verifier.')===0?'n-verify':
  (t.indexOf('recovery.')===0||t.indexOf('correction.')===0)?'n-recovery':
  t.indexOf('message.')===0?'n-msg':t.indexOf('identity.')===0?'n-ident':
  (t.indexOf('memory.')===0||t.indexOf('followup.')===0)?'n-mem':'n-life';
 const ECLS={se_origino_en:'e-caused',depende_de:'e-dep',fue_verificado_por:'e-ver',compenso_a:'e-comp'};
 const els=[];
 G.nodes.forEach(n=>els.push({data:{id:n.id,label:(n.label||n.type).slice(0,34),tipo:n.type,ts:n.ts},classes:CLS(n.type)}));
 G.edges.forEach(e=>els.push({data:{source:e.source,target:e.target,rel:e.rel},classes:ECLS[e.rel]||'e-caused'}));
 const cy=cytoscape({container:wrap,elements:els,wheelSensitivity:.25,
  style:[
   {selector:'node',style:{'shape':'round-rectangle','background-color':'#334155',
    'border-width':1,'border-color':'rgba(148,163,184,.28)','label':'data(label)',
    'color':'#e6edf6','font-size':10,'font-family':'Inter,sans-serif','font-weight':'600',
    'text-valign':'center','text-halign':'center','text-wrap':'wrap',
    'width':'label','height':'label','padding':'9','min-width':'70px'}},
   {selector:'node.n-tool',style:{'background-color':'#0f766e'}},
   {selector:'node.n-verify',style:{'background-color':'#15803d'}},
   {selector:'node.n-recovery',style:{'background-color':'#b45309'}},
   {selector:'node.n-msg',style:{'background-color':'#6d28d9'}},
   {selector:'node.n-ident',style:{'background-color':'#475569'}},
   {selector:'node.n-mem',style:{'background-color':'#7e22ce'}},
   {selector:'edge',style:{'width':1.8,'curve-style':'bezier','target-arrow-shape':'triangle',
    'arrow-scale':.8,'line-color':'#64748b','target-arrow-color':'#64748b'}},
   {selector:'edge.e-dep',style:{'line-color':'#38bdf8','target-arrow-color':'#38bdf8','line-style':'dashed'}},
   {selector:'edge.e-ver',style:{'line-color':'#34d399','target-arrow-color':'#34d399'}},
   {selector:'edge.e-comp',style:{'line-color':'#f87171','target-arrow-color':'#f87171','line-style':'dashed'}}
  ],
  layout:{name:'dagre',rankDir:'LR',nodeSep:36,rankSep:95,directed:true,padding:20}});
 cy.on('tap','node',function(ev){
  const d=ev.target.data(),info=document.getElementById('node-info');
  info.innerHTML='';
  const h=document.createElement('h3');h.textContent=d.label;info.appendChild(h);
  const b=document.createElement('span');b.className='badge b-info';b.textContent=d.tipo;
  info.appendChild(b);info.appendChild(document.createElement('br'));
  const rel=[];
  ev.target.connectedEdges().forEach(e=>{
   rel.push((e.target().id()===d.id?'← ':'→ ')+e.data('rel'));});
  const pre=document.createElement('pre');
  pre.textContent=JSON.stringify({id:d.id,ts:d.ts,relations:rel},null,2);
  info.appendChild(pre);
 });
})();
</script>
{% endblock %}'''

_T["cases.html"] = """{% extends "base.html" %}{% block title %}decisions{% endblock %}
{% block content %}<h1>Human decision queue</h1>
<p class="muted">Before executing any action, Vexinter re-checks
the real state (§5.4): use «Check current state» before
deciding; execution re-verifies again on its own.</p>
{% for c in cases %}
<div class="panel" id="case-{{ c.id }}">
<b>Case {{ c.id[:18] }}…</b>
{% if c.objective %}<span class="muted"> · {{ c.objective }}</span>{% endif %}
<table><tr><th>Tool</th><th>Originating verdict</th>
<th>Attempts</th><th>Created</th></tr>
<tr><td>{{ c.tool or "—" }}</td>
<td><span class="badge {{ c.verdict_cls }}">{{ c.verdict }}</span></td>
<td>{{ c.attempts }}</td><td class="muted">{{ c.created_at }}</td></tr></table>
<b class="muted">Evidence</b>
<table>{% for ev in c.evidence %}<tr>
<td class="muted">{{ ev.ts }}</td>
<td><span class="badge b-info">{{ ev.type }}</span></td>
<td class="muted">{{ ev.label }}</td></tr>
{% endfor %}</table>
<div id="pv-{{ c.id }}" class="muted" style="margin:6px 0"></div>
<button onclick="preview('{{ c.id }}', this)">Check current state</button>
{% for o in c.options %}
<form class="inline" method="post"
action="/console/cases/{{ c.id }}/decide" style="margin-left:8px"
onsubmit="return confirm('Execute «{{ o.action }}» (risk {{ o.risk }})?')">
<input type="hidden" name="action" value="{{ o.action }}">
<input type="hidden" name="operator" value="console">
{% if o.risk == 'alto' %}<button class="danger">{{ o.action }} ⚠</button>
{% else %}<button>{{ o.action }}</button>{% endif %}</form>
{% endfor %}</div>
{% else %}<div class="okbox">No cases pending human decision.</div>
{% endfor %}
<script>
async function preview(id, btn) {
  btn.disabled = true;
  const el = document.getElementById('pv-' + id);
  try {
    const r = await fetch('/console/api/cases/' + id + '/preview');
    const j = await r.json();
    el.textContent = 'Current state: ';
    const b = document.createElement('b');
    b.textContent = String(j.status_now);
    el.appendChild(b);
    if (j.would_skip_execution) {
      el.appendChild(document.createTextNode(
        ' — the action would NOT be executed: the state already complies (§5.4)'));
    }
    if (j.detail) {
      const d = document.createElement('span');
      d.className = 'muted';
      d.textContent = ' (' + j.detail + ')';
      el.appendChild(d);
    }
  } catch (e) { el.textContent = 'error querying: ' + e; }
  btn.disabled = false;
}
</script>
{% endblock %}"""

_T["agents.html"] = """{% extends "base.html" %}{% block title %}agents{% endblock %}
{% block content %}<h1>Agents</h1>
<div class="grid2">
<div class="panel"><b>Register agent</b>
<form method="post" action="/console/agents/enroll">
<label>Identifier</label><input name="agent_id" required
placeholder="billing_agent">
<label>Display name</label><input name="display_name" placeholder="">
<label>Allowed tools (CSV; empty = none)</label>
<input name="allowed_tools" placeholder="crm.create_customer">
<label>Allowed recipients (CSV; empty = none)</label>
<input name="allowed_recipients" placeholder="agent_b,agent_c">
<button style="margin-top:12px">Register</button></form></div>
<div class="panel"><b>Registered agents</b>
<table><tr><th>Agent</th><th>Tools</th><th>Recipients</th></tr>
{% for a in agents %}<tr><td><b>{{ a.agent_id }}</b>
{% if a.display_name %}<div class="muted">{{ a.display_name }}</div>{% endif %}
</td><td class="muted">{{ a.tools }}</td>
<td class="muted">{{ a.recipients }}</td></tr>
{% else %}<tr><td colspan="3" class="muted">no agents</td></tr>{% endfor %}
</table></div></div>
<div class="panel"><b>Active and recent credentials</b>
<p class="muted">Vexinter only knows PUBLIC keys (§14); private
ones never leave the agent.</p>
<table><tr><th>Credential</th><th>Agent</th><th>Instance</th>
<th>Status</th><th>Expires</th><th></th></tr>
{% for c in creds %}<tr><td class="muted">{{ c.short_id }}</td>
<td>{{ c.agent_id }}</td><td class="muted">{{ c.short_inst }}</td>
<td>{% if c.active %}<span class="badge b-ok">active</span>
{% else %}<span class="badge b-bad">revoked</span>{% endif %}</td>
<td class="muted">{{ c.expires }}</td>
<td>{% if c.active %}<form class="inline" method="post"
action="/console/agents/credentials/{{ c.id }}/revoke">
<input type="hidden" name="reason" value="revoked from console">
<button class="danger">Revoke</button></form>{% endif %}</td></tr>
{% else %}<tr><td colspan="6" class="muted">no credentials</td></tr>
{% endfor %}</table></div>
<div class="panel"><b>Instances</b>
<table><tr><th>Instance</th><th>Agent</th><th>Status</th><th></th></tr>
{% for i in insts %}<tr><td class="muted">{{ i.short_id }}</td>
<td>{{ i.agent_id }}</td>
<td>{% if i.active %}<span class="badge b-ok">active</span>
{% else %}<span class="badge b-bad">revoked</span>{% endif %}</td>
<td>{% if i.active %}<form class="inline" method="post"
action="/console/agents/instances/{{ i.id }}/revoke">
<input type="hidden" name="reason" value="revoked from console">
<button class="danger">Revoke</button></form>{% endif %}</td></tr>
{% else %}<tr><td colspan="4" class="muted">no instances</td></tr>
{% endfor %}</table>
<p class="muted">Revoking an instance does not affect the logical agent:
another instance can keep operating with new credentials (§9.1).</p></div>
{% endblock %}"""

_T["metrics.html"] = '''{% extends "base.html" %}{% block title %}metrics{% endblock %}
{% block content %}
<h1>Metrics</h1>
<p class="muted">Derived exclusively from the ledger (§13.5): if the ledger is the source of truth, so are the metrics.</p>
<div class="cards">
 <div class="card kpi"><div class="num">{{ metrics['executions'] }}</div><div class="lbl">Executions</div></div>
 <div class="card kpi"><div class="num" style="color:#f87171">{{ metrics['false_successes_detected'] }}</div><div class="lbl">False successes</div></div>
 <div class="card kpi"><div class="num">{{ metrics['discrepancies'] }}</div><div class="lbl">Discrepancies</div></div>
 <div class="card kpi"><div class="num">{{ metrics['retries_or_compensations'] }}</div><div class="lbl">Retries / comp.</div></div>
 <div class="card kpi"><div class="num">{{ metrics['human_decisions_requested'] }}</div><div class="lbl">Human decisions</div></div>
 <div class="card kpi"><div class="num" style="color:#fbbf24">{{ metrics.get('authentication_errors',0) }}</div><div class="lbl">Auth errors</div></div>
 <div class="card kpi"><div class="num" style="color:#f87171">{{ metrics.get('integrity_failures',0) }}</div><div class="lbl">Integrity failures</div></div>
</div>
<div class="grid2">
 <div class="panel"><b>Final states</b><div id="donut" style="height:320px"></div></div>
 <div class="panel"><b>Rejections by reason</b><div id="bars" style="height:320px"></div></div>
</div>
<script src="/console-assets/echarts.min.js"></script>
<script>
(function(){
 if(!window.echarts)return;
 const C={satisfied:'#34d399',failed:'#f87171',inconclusive:'#fb923c',provisionally_satisfied:'#eab308',
  in_progress:'#3b82f6',pending_verification:'#6366f1',pending_recovery:'#f59e0b',
  pending_human_decision:'#fb7185'};
 const E={{ estados|tojson }};
 echarts.init(document.getElementById('donut')).setOption({backgroundColor:'transparent',
  tooltip:{trigger:'item'},
  legend:{bottom:0,textStyle:{color:'#8b98ac',fontSize:11},itemWidth:10,itemHeight:10,icon:'circle'},
  series:[{type:'pie',radius:['55%','80%'],center:['50%','44%'],label:{show:false},
   itemStyle:{borderColor:'#111a2b',borderWidth:2},
   data:Object.entries(E).map(([k,v])=>({name:k,value:v,itemStyle:{color:C[k]||'#64748b'}}))}]});
 const R={{ rejects|tojson }};
 echarts.init(document.getElementById('bars')).setOption({backgroundColor:'transparent',
  tooltip:{},
  grid:{left:170,right:30,top:10,bottom:24},
  xAxis:{type:'value',axisLabel:{color:'#8b98ac'},splitLine:{lineStyle:{color:'rgba(148,163,184,.1)'}}},
  yAxis:{type:'category',data:R.map(x=>x[0]),axisLabel:{color:'#8b98ac'}},
  series:[{type:'bar',data:R.map(x=>x[1]),itemStyle:{color:'#38bdf8',borderRadius:[0,4,4,0]},barWidth:16}]});
})();
</script>
{% endblock %}'''

_env = Environment(loader=DictLoader(_T),
                   autoescape=select_autoescape(["html"]))

def _page(template: str, **ctx) -> HTMLResponse:
    return HTMLResponse(_env.get_template(template).render(**ctx))

_TRUSTED_PROXIES: list | None = None


def _trusted_networks():
    global _TRUSTED_PROXIES
    if _TRUSTED_PROXIES is None:
        import ipaddress
        nets = []
        for cidr in os.environ.get(
                "VEXINTER_TRUSTED_PROXIES", "").split(","):
            cidr = cidr.strip()
            if cidr:
                try:
                    nets.append(ipaddress.ip_network(cidr, strict=False))
                except ValueError:
                    pass
        _TRUSTED_PROXIES = nets
    return _TRUSTED_PROXIES


def client_ip(request: Request) -> str:
    """Client IP for rate limiting.

    X-Forwarded-For is trusted when the direct peer is private/loopback
    (same-host/LAN proxy) OR inside a CIDR listed in
    VEXINTER_TRUSTED_PROXIES — needed for cloud LBs (ALB, Cloudflare)
    whose egress IPs are public. A public client cannot spoof XFF
    unless it can also spoof its source address."""
    import ipaddress
    host = (request.client.host if request.client else "").strip("[]")
    try:
        peer = ipaddress.ip_address(host)
    except ValueError:
        return host or "?"
    trusted = peer.is_private or peer.is_loopback or any(
        peer in net for net in _trusted_networks())
    fwd = request.headers.get("x-forwarded-for")
    if fwd and trusted:
        return fwd.split(",", 1)[0].strip() or host
    return host or "?"


def build_console_router(rt) -> APIRouter:
    r = APIRouter()

    def guard(request: Request):
        tokens = request.app.state.console_tokens
        if tokens is None:
            request.state.operator = None
            return None
        name = _session(tokens, request.cookies.get(COOKIE))
        if name is not None:
            request.state.operator = name
            return None
        return RedirectResponse("/console/login", status_code=303)

    @r.get("/console/login")
    def login_form(request: Request, err: str = ""):
        if request.app.state.console_tokens is None:
            return RedirectResponse("/console", status_code=303)
        return _page("login.html", err=err)

    @r.post("/console/login")
    async def login(request: Request):
        form = await request.form()
        tokens = request.app.state.console_tokens
        limiter = request.app.state.login_limiter
        origen = client_ip(request)
        espera = limiter.blocked(origen)
        if espera > 0:
            resp = _page("login.html",
                         err=f"too many attempts: try again in "
                             f"{int(espera) + 1} s")
            resp.status_code = 429
            return resp
        dado = str(form.get("token") or "").encode()
        if tokens is not None:
            elegido = None
            for nombre, tok in tokens.items():
                if hmac.compare_digest(dado, tok.encode()):
                    elegido = nombre
            if elegido is not None:
                limiter.success(origen)
                redir = RedirectResponse("/console", status_code=303)
                redir.set_cookie(COOKIE, _issue(tokens[elegido], elegido),
                                 httponly=True, samesite=request.app.state.cookie_samesite,
                                 secure=request.app.state.cookie_secure,
                                 max_age=SESSION_TTL_S)
                return redir
        limiter.failure(origen)
        return _page("login.html", err="incorrect token")

    _STATE_SCAN_LIMIT = 100_000

    def _tasks_rows(state: str | None = None):
        rows = []
        created = rt.store.query(EventQuery(
            types={EventType.TASK_CREATED}, limit=500))
        # One query for ALL state transitions, grouped per execution —
        # tracker.current() per row was an N+1 (500 round-trips on PG).
        # derive_execution_state only reads EXECUTION_STATE_CHANGED events,
        # so this projection is identical to per-execution queries.
        by_exec: dict[str, list] = defaultdict(list)
        for e in rt.store.query(EventQuery(
                types={EventType.EXECUTION_STATE_CHANGED},
                limit=_STATE_SCAN_LIMIT)):
            if e.execution_id:
                by_exec[e.execution_id].append(e)
        states = {eid: derive_execution_state(evs).value
                  for eid, evs in by_exec.items()}
        for e in created:
            p = e.payload or {}
            st = states.get(e.execution_id) if e.execution_id else None
            if state and st != state:
                continue
            rows.append({"task_id": e.task_id, "agent": e.agent_id,
                         "objective": p.get("objective", ""), "state_es": st,
                         "badge": _VERDICT_BADGE.get(
                             {"satisfied": "met",
                              "provisionally_satisfied": "inconclusive",
                              "failed": "not_met",
                              "inconclusive": "inconclusive"}.get(st or "", ""),
                             "info")})
        rows.sort(key=lambda x: x["task_id"], reverse=True)
        return rows

    def _msg(request, default: str | None = None, err: bool = False):
        m = request.query_params.get("msg", default)
        return m[:300] if m else m

    @r.get("/console")
    def dashboard(request: Request):
        if (g := guard(request)) is not None:
            return g
        m = compute_metrics(rt.store)
        return _page("dashboard.html", msg=_msg(request),
                     tasks=_tasks_rows()[:8],
                     n_cases=len(rt.human_queue.pending()),
                     estados=m.get("final_states", {}), metrics=m)

    @r.get("/console/api/summary")
    def api_summary(request: Request):
        if (g := guard(request)) is not None:
            return g
        m = compute_metrics(rt.store)
        return JSONResponse({"metrics": m,
                             "cases": len(rt.human_queue.pending())})

    @r.get("/console/tasks")
    def tasks(request: Request, state: str | None = None):
        if (g := guard(request)) is not None:
            return g
        machine = [(ExecutionState(s),
                    ", ".join(sorted(t.value for t in targets)))
                   for s, targets in VALID.items()]
        for s, _ in machine:
            s.badge = _VERDICT_BADGE.get(  # type: ignore[attr-defined]
                {"satisfied": "met", "failed": "not_met",
                 "inconclusive": "inconclusive",
                 "provisionally_satisfied": "inconclusive"}.get(s.value,
                                                                    ""), "info")
        return _page("tasks.html", msg=_msg(request),
                    tasks=_tasks_rows(state), state=state,
                    states=[s.value for s in ExecutionState],
                    machine=machine)

    @r.get("/console/tasks/{task_id}")
    def task_detail(request: Request, task_id: str):
        if (g := guard(request)) is not None:
            return g
        run = rt.runs.get(task_id)
        if run is None:
            return RedirectResponse(
                "/console/tasks?msg=" + _q(f"unknown task: {task_id}"),
                status_code=303)
        evs = rt.store.query(EventQuery(task_id=task_id, limit=10_000))
        state = rt.tracker.current(run.execution_id)
        verdicts = [{
          "ts": e.ts.strftime("%H:%M:%S"),
          "status": e.payload.get("status"),
          "cls": _VERDICT_BADGE.get(e.payload.get("status"), "info"),
          "provisional": e.payload.get("provisional", False),
          "scope": e.payload.get("scope", "task"),
          "detail": ""}
          for e in evs
          if e.type == EventType.VERIFIER_VERDICT and e.payload]
        rows = []
        for e in evs:
            p = e.payload or {}
            label = next((str(p[k]) for k in
                          ("tool", "to", "status", "case_id", "strategy",
                           "message_id") if k in p), "")
            rels = []
            if e.caused_by:
                rels.append(f"← {e.caused_by[:10]}…")
            for d in e.depends_on:
                rels.append(f"⚑ {d[:10]}…")
            if e.compensates:
                rels.append(f"↩ compensates {e.compensates[:10]}…")
            rows.append({"seq": e.seq, "ts": e.ts.strftime("%H:%M:%S"),
                        "type": e.type.value, "label": _short(label, 42),
                        "rels": " ".join(rels) or "—",
                        "payload": json.dumps(p, ensure_ascii=False,
                                              default=str)[:1200]})
        audit = rt.store.verify_chain(run.stream)
        return _page("task_detail.html",
                     task_id=task_id, objective=run.objective,
                     agent=run.agent_id, state_es=state.value,
                     badge_class=_VERDICT_BADGE.get(
                         {"satisfied": "met",
                          "failed": "not_met",
                          "inconclusive": "inconclusive",
                          "provisionally_satisfied": "inconclusive"}.get(
                              state.value, ""), "info"),
                     insufficient=(state is ExecutionState.INCONCLUSIVE
                                   or any(v["status"] == "inconclusive"
                                          for v in verdicts)),
                     audit=audit, verdicts=verdicts,
                     graph=execution_graph(evs), events=rows)

    def _case_view(case):
        run = rt.runs.get(case.task_id)
        evidence = []
        for hid in case.history:
            ev = rt.store.get(hid)
            if ev is None:
                continue
            p = ev.payload or {}
            label = next((str(p[k]) for k in ("tool", "status", "strategy",
                                              "reason") if k in p), "")
            evidence.append({"ts": ev.ts.strftime("%H:%M:%S"),
                             "type": ev.type.value, "label": label[:60]})
        return {"id": case.id, "tool": case.tool,
                "verdict": case.verdict_status,
                "verdict_cls": _VERDICT_BADGE.get(case.verdict_status, "info"),
                "attempts": case.attempts,
                "created_at": case.created_at.strftime("%H:%M:%S"),
                "objective": run.objective if run else None,
                "evidence": evidence,
                "options": [{"action": o.action, "risk": o.risk}
                            for o in rt.human_queue.options_for(case.id)]}

    @r.get("/console/cases")
    def cases(request: Request):
        if (g := guard(request)) is not None:
            return g
        return _page("cases.html", msg=_msg(request),
                     cases=[_case_view(c) for c in rt.human_queue.pending()])

    @r.get("/console/api/cases/{case_id}/preview")
    def case_preview(request: Request, case_id: str):
        if (g := guard(request)) is not None:
            return g
        data = preview_case(rt, case_id)
        if data is None:
            return JSONResponse({"error": "unknown case"}, 404)
        return JSONResponse(data)

    @r.post("/console/cases/{case_id}/decide")
    async def case_decide(request: Request, case_id: str):
        if (g := guard(request)) is not None:
            return g
        form = await request.form()
        action = form.get("action", "")
        operator = (request.state.operator
                    or form.get("operator", "console"))
        case = rt.human_queue.get_case(case_id)
        if case is None:
            return RedirectResponse("/console/cases?msg=unknown+case",
                                    status_code=303)
        try:
            v = rt.human_queue.decide(case_id, action, operator)
        except StateError as e:
            return RedirectResponse(
                "/console/cases?msg=" + _q(str(e)), status_code=303)
        ev = rt.store.get(case.history[-1]) if case.history else None
        ev_payload = (ev.payload or {}) if ev else {}
        note = (ev_payload.get("note")
                or ev_payload.get("reason") or "decision executed")
        status = f"verdict after the decision: {v.status}" if v else ""
        return RedirectResponse(
            "/console/cases?msg=" + _q(f"{action}: {note}. {status}"),
            status_code=303)

    @r.get("/console/agents")
    def agents(request: Request):
        if (g := guard(request)) is not None:
            return g
        agents_rows = [{
            "agent_id": a.agent_id, "display_name": a.display_name,
            "tools": (", ".join(sorted(a.allowed_tools))
                      if a.allowed_tools is not None
                      else "ALL (development only — §14)"),
            "recipients": (", ".join(sorted(a.allowed_recipients))
                           if a.allowed_recipients is not None
                           else "ALL (development only)")}
            for a in rt.identity.list_agents()]
        creds = [{
            "id": c.credential_id, "short_id": _short(c.credential_id),
            "agent_id": c.agent_id, "short_inst": _short(c.instance_id),
            "active": c.status.value == "active",
            "expires": c.valid_until.strftime("%H:%M:%S")}
            for c in rt.identity.list_credentials()]
        insts = [{
            "id": i.instance_id, "short_id": _short(i.instance_id),
            "agent_id": i.agent_id, "active": i.status == "active"}
            for i in rt.identity.list_instances()]
        return _page("agents.html", msg=_msg(request), agents=agents_rows,
                     creds=creds, insts=insts)

    @r.post("/console/agents/enroll")
    async def agents_enroll(request: Request):
        if (g := guard(request)) is not None:
            return g
        f = await request.form()

        def csv(name):
            raw = (f.get(name) or "").strip()
            return {s.strip() for s in raw.split(",") if s.strip()}
        try:
            rt.identity.enroll_agent(f.get("agent_id", ""),
                                     display_name=f.get("display_name", ""),
                                     allowed_tools=csv("allowed_tools"),
                                     allowed_recipients=csv(
                                         "allowed_recipients"))
        except Exception as e:  # noqa: BLE001 - error boundary: converted into result/log
            return RedirectResponse("/console/agents?msg=" + _q(str(e)),
                                    status_code=303)
        return RedirectResponse("/console/agents?msg=agent+registered",
                                status_code=303)

    @r.post("/console/agents/credentials/{cid}/revoke")
    async def revoke_cred(request: Request, cid: str):
        if (g := guard(request)) is not None:
            return g
        f = await request.form()
        try:
            rt.identity.revoke_credential(cid, f.get("reason", ""))
        except Exception as e:  # noqa: BLE001 - error boundary: converted into result/log
            return RedirectResponse("/console/agents?msg=" + _q(str(e)),
                                    status_code=303)
        return RedirectResponse(
            "/console/agents?msg=credential+revoked", status_code=303)

    @r.post("/console/agents/instances/{iid}/revoke")
    async def revoke_inst(request: Request, iid: str):
        if (g := guard(request)) is not None:
            return g
        f = await request.form()
        try:
            rt.identity.revoke_instance(iid, f.get("reason", ""))
        except Exception as e:  # noqa: BLE001 - error boundary: converted into result/log
            return RedirectResponse("/console/agents?msg=" + _q(str(e)),
                                    status_code=303)
        return RedirectResponse(
            "/console/agents?msg=instance+revoked+(its+credentials+too)",
            status_code=303)

    @r.get("/console/metrics")
    def metrics(request: Request):
        if (g := guard(request)) is not None:
            return g
        m = compute_metrics(rt.store)
        rejects = sorted({**m.get("message_rejections_by_reason", {}),
                          **m.get("tool_rejections_by_reason",
                                  {})}.items(), key=lambda x: -x[1])
        return _page("metrics.html", msg=_msg(request), metrics=m,
                     estados=m.get("final_states", {}), rejects=rejects)
    return r

def mount_console(app, rt, admin_token: str | None, dev_mode: bool,
                  operators: dict | None = None) -> None:
    """Mounts the console. In dev mode it stays open. In authenticated
    mode it requires tokens (operator or admin): without them it stays
    DISABLED, not open."""
    if operators:
        # Extract plain token mapping name -> token for console cookies
        tokens = {name: (meta["token"] if isinstance(meta, dict) else meta) for name, meta in operators.items()}
    else:
        tokens = {"admin": admin_token} if admin_token else {}
    if dev_mode:
        app.state.console_tokens = None
    elif not tokens:
        import logging
        logging.getLogger("vexinter").warning(
            "console disabled: authenticated mode without admin token "
            "(VEXINTER_ADMIN_TOKEN)")
        return
    else:
        app.state.console_tokens = tokens
    app.state.login_limiter = LoginLimiter(
        max_fails=int(os.environ.get("VEXINTER_LOGIN_MAX_FAILS", "5")),
        lock_s=float(os.environ.get("VEXINTER_LOGIN_LOCK_S", "900")))
    app.state.cookie_secure = os.environ.get("VEXINTER_COOKIE_SECURE", "1") != "0"
    samesite = os.environ.get("VEXINTER_COOKIE_SAMESITE", "strict").lower()
    app.state.cookie_samesite = (
        samesite if samesite in ("strict", "lax", "none") else "strict")
    app.include_router(build_console_router(rt))

    # Vendored JS libs (echarts/cytoscape/dagre) — served locally so the
    # console works offline and no third-party CDN is in the trust path.
    from fastapi.staticfiles import StaticFiles
    vendor = Path(__file__).resolve().parent / "vendor"
    if vendor.is_dir():
        app.mount("/console-assets", StaticFiles(directory=str(vendor)),
                  name="console-assets")
