#!/usr/bin/env python3
"""Static HTML snapshot of the dashboard: bakes one ledger DB into a single
self-contained file (no server, no JavaScript fetching -- all data inlined).

    python report.py --db paper.db --out report.html [--compare other.db]

Open the HTML file in any browser. Useful when the live Flask dashboard
can't be reached (e.g. it runs on a remote VM).
"""

from __future__ import annotations

import argparse
import json

from ledger import Ledger

HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Paper Trader — report</title>
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
  <span class="pill">static snapshot</span></h1>
<div class="sub">generated __STAMP__ · strategy loops · shared risk arbiter · simulated fills</div>
<div class="cards" id="cards"></div>
<section><h2>Equity curve</h2><canvas id="eq" width="900" height="260"></canvas><div class="sub" id="eqlegend"></div></section>
<section><h2>Edge — cumulative realized P&amp;L by strategy <span style="color:#8b949e;font-weight:normal">(closed trades only)</span></h2><canvas id="edge" width="900" height="260"></canvas><div class="sub" id="edgelegend"></div>
<table id="edgetab"><tr><th>Strategy</th><th>Closed trades</th><th>Win rate</th><th>Expectancy / trade</th><th>Profit factor</th><th>Realized P&amp;L</th></tr></table></section>
<section><h2>Open positions</h2><table id="pos"><tr><th>Position</th><th>Qty</th><th>Mark</th><th>Value</th></tr></table></section>
<section><h2>Strategy P&amp;L</h2><table id="strat"><tr><th>Strategy</th><th>Trades</th><th>Win rate</th><th>Net P&amp;L</th></tr></table></section>
<section><h2>Recent trades</h2><table id="trades"><tr><th>Time</th><th>Strategy</th><th>Symbol</th><th>Action</th><th>Qty</th><th>Price</th><th>Cash Δ</th><th>Note</th></tr></table></section>
<section><h2>Risk events <span style="color:#8b949e;font-weight:normal">(orders the arbiter blocked)</span></h2><table id="risk"><tr><th>Time</th><th>Strategy</th><th>Symbol</th><th>Action</th><th>Reason</th></tr></table></section>
<script>
const DATA=__DATA__;
const fmt$=x=>'$'+Number(x).toLocaleString(undefined,{maximumFractionDigits:2});
function row(t,cells){const tr=document.createElement('tr');cells.forEach(c=>{const td=document.createElement('td');td.innerHTML=c;tr.appendChild(td)});t.appendChild(tr)}
(function(){
 const eq=DATA.equity, cmp=DATA.compare;
 const last=eq.length?eq[eq.length-1]:{equity:0,cash:0};
 const first=eq.length?eq[0].equity:last.equity;
 const ret=first?((last.equity/first-1)*100):0;
 const cls=ret>=0?'up':'down';
 document.getElementById('cards').innerHTML=
  `<div class="card"><div class="k">Equity</div><div class="v">${fmt$(last.equity)}</div></div>
   <div class="card"><div class="k">Cash</div><div class="v">${fmt$(last.cash)}</div></div>
   <div class="card"><div class="k">Return</div><div class="v ${cls}">${ret.toFixed(2)}%</div></div>
   <div class="card"><div class="k">Max drawdown</div><div class="v down">${DATA.summary.max_drawdown_pct.toFixed(1)}%</div></div>
   <div class="card"><div class="k">Data points</div><div class="v">${eq.length}</div></div>`;
 const cv=document.getElementById('eq'),cx=cv.getContext('2d');
 function idx(series){const f=series.length?series[0].equity:1;return series.map(p=>p.equity/(f||1)*100)}
 if(cmp.length){
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
 const ev=document.getElementById('edge'),ex=ev.getContext('2d');
 const COLORS=['#58a6ff','#3fb950','#f85149','#d29922','#a371f7','#39c5cf'];
 const byStrat={};
 DATA.edge_series.forEach(p=>{(byStrat[p.strategy]=byStrat[p.strategy]||[]).push(p.realized)});
 const names=Object.keys(byStrat);
 const allv=[0];names.forEach(n=>allv.push(...byStrat[n]));
 const emn=Math.min(...allv),emx=Math.max(...allv),erg=(emx-emn)||1;
 names.forEach((n,si)=>{
   const vals=byStrat[n];ex.strokeStyle=COLORS[si%COLORS.length];ex.lineWidth=2;ex.beginPath();
   vals.forEach((v,i)=>{const x=i/(vals.length-1||1)*ev.width,y=ev.height-10-((v-emn)/erg)*(ev.height-20);i?ex.lineTo(x,y):ex.moveTo(x,y)});
   ex.stroke();
 });
 const zy=ev.height-10-((0-emn)/erg)*(ev.height-20);
 ex.strokeStyle='#30363d';ex.lineWidth=1;ex.beginPath();ex.moveTo(0,zy);ex.lineTo(ev.width,zy);ex.stroke();
 document.getElementById('edgelegend').innerHTML=names.map((n,si)=>`<span style="color:${COLORS[si%COLORS.length]}">■</span> ${n}`).join(' &nbsp; ')||'no closed trades yet';
 const et=document.getElementById('edgetab');
 DATA.edge_stats.forEach(s=>{
   const n=(s.wins||0)+(s.losses||0);
   row(et,[s.strategy,n,s.win_rate==null?'—':(s.win_rate*100).toFixed(0)+'%',
     s.expectancy==null?'—':fmt$(s.expectancy),
     s.profit_factor==null?'—':s.profit_factor.toFixed(2),
     `<span class="${s.realized>=0?'up':'down'}">${fmt$(s.realized)}</span>`]);
 });
 DATA.positions.forEach(p=>row(document.getElementById('pos'),[p.pkey,p.qty,fmt$(p.mark),fmt$(p.qty*p.mark)]));
 DATA.edge_stats.forEach(s=>row(document.getElementById('strat'),[s.strategy,s.n_trades,s.win_rate==null?'—':(s.win_rate*100).toFixed(0)+'%',`<span class="${s.net>=0?'up':'down'}">${fmt$(s.net)}</span>`]));
 DATA.trades.slice(0,50).forEach(t=>row(document.getElementById('trades'),[String(t.ts).slice(0,16),t.strategy,t.symbol,t.action,t.qty,fmt$(t.price),fmt$(t.cash_delta),String(t.note||'').slice(0,60)]));
 DATA.risk.slice(0,50).forEach(r=>row(document.getElementById('risk'),[String(r.ts).slice(0,16),r.strategy,r.symbol,r.action,`<span class="pill">${r.reason}</span>`]));
})();
</script></body></html>
"""


def build(db_path: str, compare_db: str | None = None) -> str:
    from datetime import datetime, timezone
    led = Ledger(db_path)
    data = {
        "equity": led.equity_curve(),
        "compare": Ledger(compare_db).equity_curve() if compare_db else [],
        "edge_series": led.realized_series(),
        "edge_stats": led.strategy_stats(),
        "positions": led.latest_positions(),
        "trades": led.trades(limit=200),
        "risk": led.risk_events(limit=200),
        "summary": led.portfolio_summary(),
    }
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return HTML.replace("__STAMP__", stamp).replace("__DATA__", json.dumps(data))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--compare", default=None)
    args = ap.parse_args()
    html = build(args.db, args.compare)
    with open(args.out, "w") as f:
        f.write(html)
    print(f"wrote {args.out} ({len(html)//1024} KB)")


if __name__ == "__main__":
    main()
