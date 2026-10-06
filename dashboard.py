#!/usr/bin/env python3
"""Operator dashboard: one view over the paper engine's ledger.

Read-only -- it never touches the engine, the feeds, or anything live.
Run it with ``python run.py --dashboard`` after (or during) a backtest.

    python dashboard.py --db paper.db --port 5000
"""

from __future__ import annotations

import argparse
import json

from ledger import Ledger
from strategies import STRATEGY_INFO

_STRAT_ASSET = {info["class"].name: ",".join(sorted(info["class"].asset_classes))
                for info in STRATEGY_INFO.values()}

INDEX_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Paper Trader — dashboard</title>
<style>
:root{color-scheme:dark}
body{background:#0d1117;color:#e6edf3;font-family:system-ui,sans-serif;margin:0;padding:24px}
h1{font-size:22px;margin:0 0 4px} .sub{color:#8b949e;margin-bottom:20px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px 18px;min-width:150px}
.card .k{font-size:12px;color:#8b949e}.card .v{font-size:22px;font-weight:600}
.up{color:#3fb950}.down{color:#f85149}
table{width:100%;border-collapse:collapse;margin-top:8px;font-size:14px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #21262d}
th{color:#8b949e;font-weight:500}
canvas{width:100%;height:260px;background:#161b22;border:1px solid #30363d;border-radius:8px}
section{margin-bottom:28px}h2{font-size:16px;margin-bottom:8px}
.pill{display:inline-block;padding:2px 10px;border-radius:20px;font-size:12px;background:#21262d}
</style></head><body>
<h1>Paper Trader <span class="pill">paper only — no live orders</span>
  <span class="pill" id="health">checking…</span></h1>
<div class="sub">strategy loops · shared risk arbiter · simulated fills</div>
<div class="cards" id="cards"></div>
<section><h2>Equity curve</h2><canvas id="eq" width="900" height="260"></canvas><div class="sub" id="eqlegend"></div></section>
<section><h2>Edge — cumulative realized P&amp;L by strategy <span style="color:#8b949e;font-weight:normal">(closed trades only; open positions contribute on close)</span></h2><canvas id="edge" width="900" height="260"></canvas><div class="sub" id="edgelegend"></div>
<table id="edgetab"><tr><th>Strategy</th><th>Closed trades</th><th>Win rate</th><th>Expectancy / trade</th><th>Profit factor</th><th>Realized P&amp;L</th></tr></table></section>
<section><h2>Open positions</h2><table id="pos"><tr><th>Position</th><th>Qty</th><th>Mark</th><th>Value</th></tr></table></section>
<section><h2>Strategy P&amp;L</h2><table id="strat"><tr><th>Strategy</th><th>Asset class</th><th>Trades</th><th>Win rate</th><th>Net P&amp;L</th></tr></table></section>
<section><h2>Recent trades</h2><table id="trades"><tr><th>Time</th><th>Strategy</th><th>Symbol</th><th>Action</th><th>Qty</th><th>Price</th><th>Cash Δ</th><th>Note</th></tr></table></section>
<section><h2>Arbiter rejections <span style="color:#8b949e;font-weight:normal">(blocked orders by reason)</span></h2><table id="rej"><tr><th>Reason</th><th>Count</th><th>Top strategy</th></tr></table></section>
<section><h2>Risk events <span style="color:#8b949e;font-weight:normal">(orders the arbiter blocked)</span></h2><table id="risk"><tr><th>Time</th><th>Strategy</th><th>Symbol</th><th>Action</th><th>Reason</th></tr></table></section>
<script>
const fmt$=x=>'$'+Number(x).toLocaleString(undefined,{maximumFractionDigits:2});
async function j(p){return (await fetch(p)).json()}
function row(t,cells){const tr=document.createElement('tr');cells.forEach(c=>{const td=document.createElement('td');td.innerHTML=c;tr.appendChild(td)});t.appendChild(tr)}
(async()=>{
 const eq=await j('/api/equity');
 const last=eq.length?eq[eq.length-1]:{equity:0,cash:0};
 const first=eq.length?eq[0].equity:last.equity;
 const ret=first?((last.equity/first-1)*100):0;
 const cls=ret>=0?'up':'down';
 const sm=await j('/api/summary');
 document.getElementById('cards').innerHTML=
  `<div class="card"><div class="k">Equity</div><div class="v">${fmt$(last.equity)}</div></div>
   <div class="card"><div class="k">Cash</div><div class="v">${fmt$(last.cash)}</div></div>
   <div class="card"><div class="k">Return</div><div class="v ${cls}">${ret.toFixed(2)}%</div></div>
   <div class="card"><div class="k">Max drawdown</div><div class="v down">${sm.max_drawdown_pct.toFixed(1)}%</div></div>
   <div class="card"><div class="k">Data points</div><div class="v">${eq.length}</div></div>`;
 // chart
 const cv=document.getElementById('eq'),cx=cv.getContext('2d');
 const cmp=await j('/api/compare');
 function idx(series){const f=series.length?series[0].equity:1;return series.map(p=>p.equity/(f||1)*100)}
 if(cmp.length){
   // benchmark mode: both series indexed to 100 for an honest comparison
   const a=idx(eq),b=idx(cmp);
   const all=a.concat(b),mn=Math.min(...all),mx=Math.max(...all),rg=(mx-mn)||1;
   const line=(vals,color)=>{cx.strokeStyle=color;cx.lineWidth=2;cx.beginPath();
     vals.forEach((v,i)=>{const x=i/(vals.length-1||1)*cv.width,y=cv.height-10-((v-mn)/rg)*(cv.height-20);i?cx.lineTo(x,y):cx.moveTo(x,y)});cx.stroke()};
   line(a,'#58a6ff');line(b,'#d29922');
   document.getElementById('eqlegend').innerHTML='<span style="color:#58a6ff">■</span> this account (indexed) &nbsp; <span style="color:#d29922">■</span> compare account (indexed)';
 }else{
   const vals=eq.map(p=>p.equity),mn=Math.min(...vals),mx=Math.max(...vals),rg=(mx-mn)||1;
   cx.strokeStyle='#58a6ff';cx.lineWidth=2;cx.beginPath();
   vals.forEach((v,i)=>{const x=i/(vals.length-1||1)*cv.width,y=cv.height-10-((v-mn)/rg)*(cv.height-20);i?cx.lineTo(x,y):cx.moveTo(x,y)});
   cx.stroke();
 }
 // edge panel
 const edge=await j('/api/edge');
 const ev=document.getElementById('edge'),ex=ev.getContext('2d');
 const COLORS=['#58a6ff','#3fb950','#f85149','#d29922','#a371f7','#39c5cf'];
 const byStrat={};
 edge.series.forEach(p=>{(byStrat[p.strategy]=byStrat[p.strategy]||[]).push(p.realized)});
 const names=Object.keys(byStrat);
 const allv=[0];names.forEach(n=>allv.push(...byStrat[n]));
 const emn=Math.min(...allv),emx=Math.max(...allv),erg=(emx-emn)||1;
 names.forEach((n,si)=>{
   const vals=byStrat[n];ex.strokeStyle=COLORS[si%COLORS.length];ex.lineWidth=2;ex.beginPath();
   vals.forEach((v,i)=>{const x=i/(vals.length-1||1)*ev.width,y=ev.height-10-((v-emn)/erg)*(ev.height-20);i?ex.lineTo(x,y):ex.moveTo(x,y)});
   ex.stroke();
 });
 // zero line
 const zy=ev.height-10-((0-emn)/erg)*(ev.height-20);
 ex.strokeStyle='#30363d';ex.lineWidth=1;ex.beginPath();ex.moveTo(0,zy);ex.lineTo(ev.width,zy);ex.stroke();
 document.getElementById('edgelegend').innerHTML=names.map((n,si)=>`<span style="color:${COLORS[si%COLORS.length]}">■</span> ${n}`).join(' &nbsp; ');
 const et=document.getElementById('edgetab');
 edge.stats.forEach(s=>{
   const n=(s.wins||0)+(s.losses||0);
   row(et,[s.strategy,n,s.win_rate==null?'—':(s.win_rate*100).toFixed(0)+'%',
     s.expectancy==null?'—':fmt$(s.expectancy),
     s.profit_factor==null?'—':s.profit_factor.toFixed(2),
     `<span class="${s.realized>=0?'up':'down'}">${fmt$(s.realized)}</span>`]);
 });
 const pos=await j('/api/positions');
 pos.forEach(p=>row(document.getElementById('pos'),[p.pkey,p.qty,fmt$(p.mark),fmt$(p.qty*p.mark)]));
 const st=await j('/api/strategies');
 st.forEach(s=>row(document.getElementById('strat'),[s.strategy,s.asset_class,s.n_trades,s.win_rate==null?'—':(s.win_rate*100).toFixed(0)+'%',`<span class="${s.net>=0?'up':'down'}">${fmt$(s.net)}</span>`]));
 async function health(){
  const hl=await j('/api/health');
  const hel=document.getElementById('health');
  if(hl.status==='live'){hel.textContent=`LIVE · ${hl.age_s}s ago`;hel.style.background='#1a3a24';hel.style.color='#3fb950'}
  else if(hl.status==='stale'){hel.textContent=`STALE · ${Math.round(hl.age_s/60)}m ago`;hel.style.background='#3a1a1a';hel.style.color='#f85149'}
  else{hel.textContent='no live session';hel.style.background='#21262d'}
 }
 await health(); setInterval(health,15000);
 const tr=await j('/api/trades');
 tr.slice(0,50).forEach(t=>row(document.getElementById('trades'),[t.ts.slice(0,16),t.strategy,t.symbol,t.action,t.qty,fmt$(t.price),fmt$(t.cash_delta),(t.note||'').slice(0,60)]));
 const rk=await j('/api/risk');
 rk.slice(0,50).forEach(r=>row(document.getElementById('risk'),[r.ts.slice(0,16),r.strategy,r.symbol,r.action,`<span class="pill">${r.reason}</span>`]));
 const rej=await j('/api/rejections');
 Object.entries(rej.by_reason).sort((a,b)=>b[1]-a[1]).forEach(([reason,n])=>{
   const top=(rej.by_strategy_reason.find(r=>r.reason===reason)||{}).strategy||'—';
   row(document.getElementById('rej'),[`<span class="pill">${reason}</span>`,n,top]);
 });
 if(!rej.total) row(document.getElementById('rej'),['<span style="color:#8b949e">no rejections recorded</span>','','']);
})();
</script></body></html>
"""


def create_app(db_path: str, compare_db: str | None = None):
    from flask import Flask, jsonify
    app = Flask(__name__)
    led = Ledger(db_path)
    _compare_db = compare_db

    @app.get("/")
    def index():
        return INDEX_HTML

    @app.get("/api/equity")
    def api_equity():
        return jsonify(led.equity_curve())

    @app.get("/api/compare")
    def api_compare():
        """Second account's equity curve, for benchmark overlays."""
        if _compare_db:
            return jsonify(Ledger(_compare_db).equity_curve())
        return jsonify([])

    @app.get("/api/edge")
    def api_edge():
        return jsonify({"series": led.realized_series(),
                        "stats": led.strategy_stats()})

    @app.get("/api/summary")
    def api_summary():
        s = led.portfolio_summary()
        s["n_trades"] = sum(r["n_trades"] for r in led.strategy_pnl())
        s["n_risk_events"] = len(led.risk_events(limit=100000))
        return jsonify(s)

    @app.get("/api/health")
    def api_health():
        """Dead-man's switch: LIVE/STALE from the engine's heartbeat file."""
        import json as _json
        import os
        from datetime import datetime
        d = os.path.dirname(os.path.abspath(db_path)) or "."
        try:
            with open(os.path.join(d, "heartbeat.json")) as f:
                hb = _json.load(f)
            age = (datetime.now() -
                   datetime.fromisoformat(hb["ts"])).total_seconds()
            return jsonify({"status": "live" if age < 180 else "stale",
                            "age_s": round(age), "equity": hb.get("equity"),
                            "step": hb.get("step")})
        except Exception:
            return jsonify({"status": "never", "age_s": None})

    @app.get("/api/positions")
    def api_positions():
        return jsonify(led.latest_positions())

    @app.get("/api/strategies")
    def api_strategies():
        rows = led.strategy_stats()
        for r in rows:
            r["asset_class"] = _STRAT_ASSET.get(r["strategy"], "?")
            w, l = r["wins"] or 0, r["losses"] or 0
            r["win_rate"] = (w / (w + l)) if (w + l) else None
        return jsonify(rows)

    @app.get("/api/trades")
    def api_trades():
        return jsonify(led.trades())

    @app.get("/api/risk")
    def api_risk():
        return jsonify(led.risk_events())

    @app.get("/api/rejections")
    def api_rejections():
        return jsonify(led.rejection_summary())

    return app


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="paper.db")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--compare", default=None,
                    help="second ledger DB overlaid on the equity chart "
                         "(e.g. a buy-and-hold pie benchmark)")
    args = ap.parse_args()
    create_app(args.db, args.compare).run(port=args.port)


if __name__ == "__main__":
    main()
