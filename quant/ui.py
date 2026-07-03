"""The page. One question at the top — "what should I do right now?" — and
everything else folded away. All numbers arrive pre-worded from the engine
(language lint lives server-side); this layer only lays them out."""

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>QUANT</title>
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@500;700&family=IBM+Plex+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#161310;--panel:#1f1a15;--line:#3a322a;--ink:#e8e0d0;--dim:#998f7d;
--gold:#c9a86a;--up:#8aa86b;--warn:#c25e4c;--info:#6e93a8}
*{box-sizing:border-box}body{margin:0 auto;max-width:880px;padding:0 14px 60px;background:var(--bg);
color:var(--ink);font:14px/1.5 "IBM Plex Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums}
header{display:flex;gap:14px;align-items:baseline;flex-wrap:wrap;padding:16px 0 10px;border-bottom:1px solid var(--line)}
h1{font-family:Cinzel,Georgia,serif;font-size:18px;letter-spacing:.18em;margin:0;color:var(--gold)}
.k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.12em}
.v{font-weight:600}.gold{color:var(--gold)}.pos{color:var(--up)}.neg{color:var(--warn)}
.chip{border:1px solid var(--info);color:var(--info);font-size:10px;padding:2px 7px;letter-spacing:.15em}
.chip.real{border-color:var(--warn);color:var(--warn)}
#dot{width:10px;height:10px;border-radius:50%;display:inline-block;vertical-align:-1px;background:var(--up);cursor:help}
#dot.amber{background:var(--gold)} #dot.red{background:var(--warn)}
#trust{padding:12px 2px;border-bottom:1px solid var(--line);color:var(--ink)}
#grad{padding:8px 2px;color:var(--dim);font-size:13px}
.card{border:1px solid var(--line);border-left:4px solid var(--up);background:var(--panel);padding:12px 16px;margin:10px 0}
.card.SELL{border-left-color:var(--gold)}.card.ABANDON{border-left-color:var(--warn)}
.card.HOLD,.card.CHECK{border-left-color:var(--line);opacity:.85}
.card .head{font-weight:600;font-size:15px}.card .plan{margin-top:3px}
.card .why{margin-top:5px;color:var(--dim);font-size:13px}
.card button{margin-top:10px}
.card .resting{color:var(--info);font-size:13px;margin-top:8px}
#notrade{border:1px solid var(--line);background:var(--panel);padding:22px;text-align:center;color:var(--dim);margin:12px 0}
#notrade b{color:var(--ink);font-size:15px;display:block;margin-bottom:6px}
button{background:none;border:1px solid var(--gold);color:var(--gold);font:inherit;padding:6px 14px;cursor:pointer}
button:hover{background:rgba(201,168,106,.12)}button.small{font-size:12px;padding:3px 9px}
a{color:var(--info);text-decoration:none;cursor:pointer}
details{margin:14px 0}summary{cursor:pointer;color:var(--dim);font-size:12px;letter-spacing:.18em;text-transform:uppercase}
section{border:1px solid var(--line);background:var(--panel);padding:12px 14px;margin-top:10px}
h2{font-family:Cinzel,serif;font-size:12px;letter-spacing:.2em;color:var(--dim);margin:0 0 10px;font-weight:500}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:5px 6px;border-bottom:1px solid var(--line);text-align:right}
td:first-child,th:first-child{text-align:left}
input,select{background:var(--bg);border:1px solid var(--line);color:var(--ink);font:inherit;padding:5px 7px;width:100%}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px}
.dettbl{margin-top:8px;font-size:12px;color:var(--dim)}
#toast{position:fixed;right:18px;bottom:18px;background:var(--panel);border:1px solid var(--up);
padding:10px 14px;opacity:0;transition:opacity .3s;pointer-events:none;z-index:9;max-width:360px}
#toast.show{opacity:1}
#debrief{border:1px solid var(--info);background:var(--panel);padding:10px 14px;margin:12px 0;font-size:13px}
#updbanner{border:1px solid var(--info);background:var(--panel);padding:10px 14px;margin:12px 0;font-size:13px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
#status{color:var(--dim);font-size:12px;padding:4px 2px 8px;letter-spacing:.04em}
#status b{color:var(--ink)}
.editing input,.editing select{border-color:var(--gold)}
.acbox{position:fixed;background:var(--panel);border:1px solid var(--gold);max-height:240px;overflow:auto;z-index:50;font-size:13px;box-shadow:0 6px 18px rgba(0,0,0,.5)}
.acitem{padding:6px 10px;cursor:pointer;white-space:nowrap}
.acitem:hover,.acitem.sel{background:rgba(201,168,106,.2)}
.matchbox input{max-width:240px}
.cardx{position:absolute;top:4px;right:9px;color:var(--dim);font-size:20px;line-height:1;text-decoration:none}
.cardx:hover{color:var(--warn)}
.confirm{display:grid;grid-template-columns:1fr 1fr auto;gap:8px;margin-top:10px}
svg{display:block;width:100%;height:60px;margin-top:8px}
</style></head><body>
<header><h1>QUANT</h1><span class="k" id="league"></span><span class="chip" id="mode"></span>
<span><span class="k">net worth </span><span class="v gold" id="nw">—</span><span class="k"> div</span>
<span class="v" id="delta"></span></span>
<span style="margin-left:auto"><span id="dot" title=""></span>
<button class="small" id="refresh" style="margin-left:10px">refresh</button></span></header>
<div id="updbanner" hidden></div>
<div id="trust"><span id="trustline">…</span> <a id="trusttog">details ▸</a>
<div id="trustdetail" hidden></div></div>
<div id="grad"></div>
<div id="debrief" hidden></div>
<div id="status"></div>
<div id="cards"></div>

<details id="record"><summary>Record — orders, trades, capital</summary>
<section><h2>Resting orders <button class="small" id="cancelall" hidden style="float:right">cancel all</button></h2><table id="orders"><tbody></tbody></table></section>
<section><h2>Trades</h2><table id="trades"><thead><tr><th>when</th><th>item</th><th>side</th>
<th>qty</th><th>ex / unit</th><th></th><th></th></tr></thead><tbody></tbody></table>
<div class="grid" id="fillform" style="margin-top:10px">
<input id="f_item" placeholder="start typing — pick from the list" autocomplete="off">
<select id="f_side"><option>buy</option><option>sell</option></select>
<input id="f_qty" placeholder="qty" inputmode="decimal"><input id="f_px" placeholder="price PER UNIT (ex)" inputmode="decimal">
<button class="small" id="f_go">record fill</button><button class="small" id="f_cancel" hidden>cancel edit</button></div>
<p class="k" id="f_hint" style="margin:8px 0 0">Price is <b>per unit</b>, in exalted — the same "ex each" number the card shows, not the order total. Click <b>edit</b> on any trade to fix one; everything recalculates.</p>
<p class="k" id="f_match" style="margin:4px 0 0"></p></section>
<section><h2>Capital — what you hold right now (liquid, not positions)</h2>
<div class="grid"><input id="c_div" placeholder="divine" inputmode="decimal">
<input id="c_ex" placeholder="exalted" inputmode="decimal">
<input id="c_chaos" placeholder="chaos" inputmode="decimal">
<button class="small" id="c_go">set holdings</button></div>
<div class="k" id="capnow" style="margin-top:8px"></div></section></details>

<details id="engine"><summary>Engine room — evidence, candidates, settings</summary>
<section><h2>Benchmarks (your net worth must beat all three)</h2><table id="bench"><tbody></tbody></table>
<svg id="spark"></svg><div class="k">net worth, current mode</div></section>
<section><h2>Signals — measured, not assumed</h2><table id="sigs"><thead>
<tr><th>signal</th><th>graded</th><th>hit pred→real</th><th>avg edge</th><th>state</th></tr></thead><tbody></tbody></table>
<div class="k" id="gatenote" style="margin-top:6px">gated signals keep shadow-trading; they earn their way back with evidence</div>
<div id="relwrap" hidden style="margin-top:10px"><div class="k">model reliability — does a higher model estimate actually mean a higher hit rate?</div>
<table id="rel"><thead><tr><th>signal</th><th>model band</th><th>n</th><th>est</th><th>real</th></tr></thead><tbody></tbody></table></div>
<div id="touchwrap" hidden style="margin-top:10px"><div class="k">price-only reliability — did price reach target within H, unconditional on fill (diagnostic: selection-biased easy, never sized)</div>
<table id="touch"><thead><tr><th>signal</th><th>model band</th><th>n</th><th>est</th><th>real touch</th></tr></thead><tbody></tbody></table></div>
<div id="featwrap" hidden style="margin-top:10px"><div class="k">feature reliability — which raw signals separate winners from losers (diagnostic only, never sizes)</div>
<table id="feat"><thead><tr><th>signal</th><th>feature</th><th>bucket</th><th>n</th><th>real hit</th></tr></thead><tbody></tbody></table></div>
<div id="fillwrap" hidden style="margin-top:10px"><div class="k">fill rate by time — do resting orders fill in thin hours? (diagnostic only)</div>
<table id="fillh"><thead><tr><th>window</th><th>n</th><th>p_fill</th><th>real fill</th></tr></thead><tbody></tbody></table></div></section>
<section><h2>Top candidates this poll</h2><table id="scan"><thead>
<tr><th>item</th><th>sig</th><th>EV %</th><th>P(hit)</th><th>vol div/d</th></tr></thead><tbody></tbody></section>
<section><h2>Pinned theses</h2><table id="pins"><tbody></tbody></table></section>
<section><h2>Settings</h2>
<div class="grid"><select id="s_risk"><option>conservative</option><option>standard</option><option>aggressive</option></select>
<select id="s_mode"><option>paper</option><option>real</option></select>
<button class="small" id="s_go">apply</button><button class="small" id="notif">enable exit alerts</button></div>
<p class="k" id="gradnote" style="margin-top:8px"></p>
<div class="grid" style="margin-top:6px"><button class="small" id="up_check">check for updates</button>
<span class="k" id="up_status" style="align-self:center;grid-column:2/4"></span></div>
<p class="k" id="verline" style="margin-top:6px"></p>
<p class="k">advanced knobs: config.advanced.json (none required)</p></section>
<section><h2>Diagnostics</h2><div id="diag" class="k"></div></section></details>
<div id="toast"></div>
<script>
const TOKEN="__TOKEN__";const $=s=>document.querySelector(s);let D=null;
let EDITING=null,FILLS={};
const hdrs=TOKEN?{"X-Quant-Token":TOKEN}:{ };
function esc(s){return String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function toast(m){const t=$("#toast");t.textContent=m;t.classList.add("show");
clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove("show"),3200)}
async function api(p,b){const r=await fetch(p,{method:b?"POST":"GET",
headers:b?{...hdrs,"Content-Type":"application/json"}:hdrs,body:b?JSON.stringify(b):undefined});
let j=null;try{j=await r.json()}catch(_){}
if(!r.ok)throw new Error((j&&j.err)||("server error "+r.status));return j||{}}
function num(v){return parseFloat(String(v==null?"":v).replace(",","."))}
function cls(n){return n>0?"pos":n<0?"neg":""}
function dett(d){return "<table class='dettbl'>"+Object.entries(d).map(([k,v])=>
`<tr><td>${esc(k)}</td><td>${esc(typeof v==="number"?v:String(v))}</td></tr>`).join("")+"</table>"}

function renderCards(snap,orders){
const wrap=$("#cards");const byCard={};(orders||[]).forEach(o=>{if(o.card_id)byCard[o.card_id]=o});
const cards=snap.cards||[];
if(!cards.length||cards.every(c=>c.act==="HOLD")){
const nt=snap.no_trade||{};wrap.innerHTML=`<div id="notrade"><b>${esc(nt.line||"Nothing worth your divines right now.")}</b>
${nt.checked??"?"} items checked · entries never notify — the next dip always comes</div>`
+cards.map(c=>cardHTML(c,byCard)).join("");}
else wrap.innerHTML=cards.map(c=>cardHTML(c,byCard)).join("");
wrap.querySelectorAll("[data-take]").forEach(b=>b.onclick=()=>take(b));
wrap.querySelectorAll("[data-close]").forEach(b=>b.onclick=()=>prefillClose(b.dataset.item,b.dataset.qty));
wrap.querySelectorAll("[data-mtoggle]").forEach(a=>a.onclick=()=>{
const mb=a.closest(".matchbox"),box=mb&&mb.querySelector("[data-mbox]");if(!box)return;
box.hidden=!box.hidden;if(!box.hidden){const inp=box.querySelector("[data-minput]");attachAC(inp,()=>{});inp.focus();}});
wrap.querySelectorAll("[data-mgo]").forEach(b=>b.onclick=()=>{
const box=b.closest("[data-mbox]"),inp=box&&box.querySelector("[data-minput]");renameItem(b.dataset.item,inp?inp.value:"");});
wrap.querySelectorAll("[data-det]").forEach(a=>a.onclick=()=>{const d=a.nextElementSibling;d.hidden=!d.hidden});
wrap.querySelectorAll("[data-discard]").forEach(a=>a.onclick=()=>discardItem(a.dataset.discard));
wrap.querySelectorAll("[data-cancel]").forEach(a=>a.onclick=async()=>{
await api("/api/void",{id:+a.dataset.cancel,kind:"order"});toast("order cancelled");load()});
// exit alerts: SELL/ABANDON are the only time-sensitive thing this app says
const urgent=cards.filter(c=>c.act==="SELL"||c.act==="ABANDON").map(c=>c.id);
const seen=JSON.parse(localStorage.getItem("q_urgent")||"[]");
urgent.filter(id=>!seen.includes(id)).forEach(id=>{const c=cards.find(x=>x.id===id);
if(Notification.permission==="granted")new Notification("QUANT: "+c.act,{body:c.head});});
localStorage.setItem("q_urgent",JSON.stringify(urgent));}

function cardHTML(c,byCard){
const o=byCard[c.id];const paper=D.cfg.mode==="paper";
let btn="";
if(o)btn=`<div class="resting">order resting at ${o.px} ex — fills when the market trades through
· <a data-cancel="${o.id}">cancel</a></div>`;
else if(c.act==="HOLD"||c.act==="CHECK"){
btn=c.closeable?`<button class="small" data-close="1" data-item="${esc(c.item)}" data-qty="${c.qty}">I sold it — log the sale</button>`:"";
if(c.act==="CHECK")btn+=`<div class="matchbox" style="margin-top:8px"><a data-mtoggle="1">match to a scanned item ▸</a>`
+`<div data-mbox hidden style="margin-top:6px;display:flex;gap:6px"><input data-minput placeholder="type the real item name" autocomplete="off">`
+`<button class="small" data-mgo data-item="${esc(c.item)}">match</button></div></div>`;}
else{const side=(c.act==="SELL"||c.act==="ABANDON")?"sell":"buy";
const label=paper?(c.act==="ABANDON"?"Sell now (paper)":"Take it (paper)"):"I did it — log the fill";
btn=`<button data-take="1" data-id="${esc(c.id)}" data-item="${esc(c.item)}" data-side="${side}"
data-qty="${c.qty}" data-px="${side==="sell"?(c.target_px??c.px):c.px}" data-tgt="${c.target_px??""}"
data-sig="${esc(c.sig||c.act)}" data-act="${c.act}">${label}</button>
<div class="confirm" hidden><input value="${c.qty}" placeholder="qty" title="how many" inputmode="decimal"><input value="${side==="sell"?(c.target_px??c.px):c.px}" placeholder="ex per unit" title="price per unit in exalted" inputmode="decimal">
<button class="small">confirm</button></div>`;}
const x=(c.act==="CHECK"||c.act==="HOLD")?`<a class="cardx" data-discard="${esc(c.item)}" title="stop tracking this">×</a>`:"";
return `<div class="card ${c.act}" style="position:relative">${x}<div class="head">${esc(c.head)}</div>
<div class="plan">${esc(c.plan||"")}</div>
<div class="why">${esc(c.why||"")} ${c.det?`<a data-det="1">details ▸</a><div hidden>${dett(c.det)}</div>`:""}</div>${btn}</div>`}

async function take(b){
const paper=D.cfg.mode==="paper";const d=b.dataset;
const card=((D.snap&&D.snap.cards)||[]).find(x=>x.id===d.id)||{};
const txt={head:card.head,plan:card.plan,why:card.why};  // kept on the order so it can be reopened later
try{
if(paper){b.disabled=true;
const instant=d.act==="ABANDON";
await api("/api/take",{card_id:d.id,item:d.item,side:d.side,qty:num(d.qty),px:num(d.px),
target_px:d.tgt?num(d.tgt):null,sig:d.sig,ledger:"paper",instant,...txt});
toast(instant?"sold at market (paper)":"resting order set — it fills only when the market actually trades through");load();}
else{const c=b.nextElementSibling;if(c.hidden){c.hidden=false;return}
const[q,p]=c.querySelectorAll("input");const qty=num(q.value),px=num(p.value);
if(!(qty>0)||!(px>0))return toast("enter a positive qty and a per-unit price");
await api("/api/take",{card_id:d.id,item:d.item,side:d.side,qty,px,
target_px:d.tgt?num(d.tgt):null,sig:d.sig,ledger:"real",...txt});
toast("fill recorded — your real numbers, not the card's");load();}
}catch(e){b.disabled=false;toast("couldn't record: "+e.message)}}

function render(){
renderUpdate(D.update);
const s=D.snap;if(!s){$("#cards").innerHTML="<div id='notrade'><b>First poll running…</b>refresh in a moment</div>";return}
$("#league").textContent=s.league||"";document.title="QUANT · "+(s.league||"");
$("#mode").textContent=D.cfg.mode.toUpperCase();$("#mode").className="chip "+D.cfg.mode;
const port=s.port||{};$("#nw").textContent=port.nw_div??"—";
const ds=port.deltas||{};const worst=Math.min(...Object.values(ds).length?Object.values(ds):[NaN]);
$("#delta").textContent=isFinite(worst)?` ${worst>=0?"+":""}${worst.toFixed(2)} vs worst benchmark`:"";
$("#delta").className="v "+cls(worst);
const age=(Date.now()-Date.parse(s.ts))/60000;
const dot=$("#dot");dot.className=age>20?"red":(s.errors||[]).length?"amber":"";dot.id="dot";
dot.title=`data ${Math.round(age)} min old`+((s.errors||[]).length?` · ${s.errors.length} warning(s) — see diagnostics`:"");
$("#trustline").textContent=s.trust||"";
$("#grad").textContent=(s.grad&&s.grad.line)||"";
$("#gradnote").textContent=(s.grad&&s.grad.line)||"";
renderCards(s,D.orders);
renderStatus(s.status);
$("#orders tbody").innerHTML=(D.orders||[]).map(o=>{
const tgt=o.target_px?`sell at <b>${o.target_px}</b> ex`:"<span class='k'>no target stored</span>";
const det=(o.head||o.plan||o.why)
 ?`${esc(o.head||"")}${o.plan?"<br>"+esc(o.plan):""}${o.why?"<br><span class='k'>"+esc(o.why)+"</span>":""}`
 :`bid ${o.qty}× ${esc(o.item)} at ${o.px} ex`+(o.target_px?` → when it fills, sell at ${o.target_px} ex`:"")
   +(o.sig?` · signal ${esc(o.sig)}`:"")+(o.ts?` · placed ${esc(o.ts.slice(5,16).replace("T"," "))}`:"");
return `<tr><td>${esc(o.item)}</td><td>${o.side} ${o.qty} @ ${o.px} ex</td><td>${tgt}</td>
<td><a data-od="${o.id}">show card ▸</a></td><td><a data-cancel="${o.id}">cancel</a></td></tr>
<tr id="od${o.id}" hidden><td colspan="5" style="background:rgba(0,0,0,.18)">${det}
<div style="margin-top:8px">filled <input data-oqty value="${o.qty}" inputmode="decimal" style="width:64px"> of ${o.qty} ·
<button class="small" data-obuy="${o.id}">I bought it</button>
<button class="small" data-osell="${o.id}">I sold it</button></div></td></tr>`}).join("")
||"<tr><td class='k'>none</td></tr>";
$("#orders").querySelectorAll("[data-od]").forEach(a=>a.onclick=()=>{const r=$("#od"+a.dataset.od);if(r)r.hidden=!r.hidden});
$("#orders").querySelectorAll("[data-cancel]").forEach(a=>a.onclick=async()=>{
await api("/api/void",{id:+a.dataset.cancel,kind:"order"});toast("order cancelled");load()});
$("#orders").querySelectorAll("[data-obuy]").forEach(b=>b.onclick=()=>{
const qi=b.parentElement.querySelector("[data-oqty]");orderBought(b.dataset.obuy,qi?qi.value:null)});
$("#orders").querySelectorAll("[data-osell]").forEach(b=>b.onclick=()=>{
const qi=b.parentElement.querySelector("[data-oqty]");orderSold(b.dataset.osell,qi?qi.value:null)});
$("#cancelall").hidden=!(D.orders||[]).length;
FILLS={};(D.fills||[]).forEach(f=>FILLS[f.id]=f);
$("#trades tbody").innerHTML=(D.fills||[]).map(f=>`<tr><td>${esc(f.ts.slice(5,16).replace("T"," "))}</td>
<td>${esc(f.item)}</td><td>${f.side}</td><td>${f.qty}</td><td>${f.px}</td>
<td class="k">${f.ledger}</td><td><a data-edit="${f.id}">edit</a> · <a data-void="${f.id}">void</a></td></tr>`).join("")
||"<tr><td colspan=7 class='k'>no fills yet</td></tr>";
$("#trades").querySelectorAll("[data-edit]").forEach(a=>a.onclick=()=>{const f=FILLS[a.dataset.edit];if(f)startEdit(f)});
$("#trades").querySelectorAll("[data-void]").forEach(a=>a.onclick=async()=>{
if(confirm("Void fill #"+a.dataset.void+"? (a correction event is appended; nothing is rewritten)"))
{await api("/api/void",{id:+a.dataset.void});toast("voided");load()}});
const H=(s.holdings||{});$("#capnow").textContent=H.ts?
`set ${H.ts.slice(0,16).replace("T"," ")} → ${H.div||0} div + ${H.ex||0} ex + ${H.chaos||0} chaos`:
"not set — paper uses a notional bankroll; set holdings before going real";
const b=port.bench||{};$("#bench tbody").innerHTML=Object.entries(b).map(([k,v])=>
`<tr><td>${k.replace("_"," ")}</td><td>${v} div</td><td class="${cls(ds[k])}">${ds[k]>=0?"+":""}${ds[k]??"—"}</td></tr>`).join("")
||"<tr><td class='k'>needs a few polls</td></tr>";
const sb=s.scoreboard||{},g=s.gates||{};
$("#sigs tbody").innerHTML=Object.entries(sb).map(([k,v])=>`<tr><td>${k}</td><td>${v.n}</td>
<td>${v.hit_pred??"—"}→${v.hit_freq??"—"}</td><td class="${cls(v.edge_mean_pct)}">${v.edge_mean_pct??"—"}%</td>
<td>${g[k]&&g[k].off?"GATED OFF (edge ≤ 0, n="+g[k].n+")":"live"}</td></tr>`).join("")
||"<tr><td colspan=5 class='k'>no graded forecasts yet — the shadow book is collecting them</td></tr>";
$("#trustdetail").innerHTML=dett(Object.fromEntries(Object.entries(sb).map(([k,v])=>
[k,`n=${v.n} fill ${v.fill_freq??"—"} hit ${v.hit_freq??"—"} edge ${v.edge_mean_pct??"—"}% crps ${v.crps??"—"}`])));
const rel=s.reliability||{};const relRows=[];
Object.entries(rel).forEach(([k,v])=>(v.buckets||[]).forEach(b=>relRows.push(
`<tr><td>${k}</td><td>${b.lo}–${b.hi}</td><td>${b.n}</td><td>${b.p_mean}</td><td class="${cls((b.freq-b.p_mean))}">${b.freq}</td></tr>`)));
$("#relwrap").hidden=!relRows.length;
$("#rel tbody").innerHTML=relRows.join("");
const tr=s.touch_rel||{};const trRows=[];
Object.entries(tr).forEach(([k,v])=>(v.buckets||[]).forEach(b=>trRows.push(
`<tr><td>${k}</td><td>${b.lo}–${b.hi}</td><td>${b.n}</td><td>${b.p_mean}</td><td class="${cls((b.freq-b.p_mean))}">${b.freq}</td></tr>`)));
$("#touchwrap").hidden=!trRows.length;
$("#touch tbody").innerHTML=trRows.join("");
const fr=s.feature_rel||{};const frRows=[];
Object.entries(fr).forEach(([sig,v])=>Object.entries(v.feats||{}).forEach(([fn,bs])=>bs.forEach(b=>frRows.push(
`<tr><td>${sig}</td><td>${fn}</td><td>${esc(b.label)}</td><td>${b.n}</td><td>${b.freq}</td></tr>`))));
$("#featwrap").hidden=!frRows.length;
$("#feat tbody").innerHTML=frRows.join("");
const fh=s.fill_hours||{};const fhRows=[];
(fh.utc_band||[]).forEach(b=>fhRows.push(`<tr><td>${b.band} UTC</td><td>${b.n}</td><td>${b.p_fill_mean}</td><td class="${cls(b.fill_freq-b.p_fill_mean)}">${b.fill_freq}</td></tr>`));
(fh.day||[]).forEach(b=>fhRows.push(`<tr><td>${b.day}</td><td>${b.n}</td><td>${b.p_fill_mean}</td><td class="${cls(b.fill_freq-b.p_fill_mean)}">${b.fill_freq}</td></tr>`));
$("#fillwrap").hidden=!fhRows.length;
$("#fillh tbody").innerHTML=fhRows.join("");
$("#scan tbody").innerHTML=(s.scan||[]).map(r=>`<tr><td>${esc(r.item)}</td><td>${r.sig}</td>
<td>${r.ev_pct}</td><td>${r.p_hit}</td><td>${r.vol_div}</td></tr>`).join("")
||"<tr><td colspan=5 class='k'>nothing passed</td></tr>";
$("#pins tbody").innerHTML=(s.pins||[]).map(p=>`<tr><td>${esc(p.label)}</td><td>${p.px??"no match"}</td>
<td class="k">entry≤${p.entry??"—"} exit≥${p.exit??"—"}</td></tr>`).join("")||"<tr><td class='k'>none — add pins in config.json</td></tr>";
$("#s_risk").value=D.cfg.risk;$("#s_mode").value=D.cfg.mode;
const st=s.stats||{};$("#diag").innerHTML=[`scanned ${st.scanned} items · ${st.proposals} proposals · shadow book ${st.shadow_open} open · ${st.graded_30d} graded/30d`,
`market move ${s.market_z} of normal${s.circuit?" — CIRCUIT BREAKER: entries paused":""} · basket index ${st.index}`,
...(s.errors||[]).map(e=>"warn: "+esc(e))].join("<br>");
const h=(D.hist||[]).filter(x=>x.nw!=null);if(h.length>1){const ys=h.map(p=>p.nw);
const mn=Math.min(...ys),mx=Math.max(...ys);
const pts=h.map((p,i)=>`${i/(h.length-1)*100},${56-((p.nw-mn)/((mx-mn)||1))*48-4}`).join(" ");
$("#spark").innerHTML=`<polyline points="${pts}" fill="none" stroke="#c9a86a" stroke-width="1.5"/>`;
$("#spark").setAttribute("viewBox","0 0 100 56");$("#spark").setAttribute("preserveAspectRatio","none");}
const last=localStorage.getItem("q_seen");
if(last&&(Date.now()-Date.parse(last))>6*3600*1000){
api("/api/debrief?since="+encodeURIComponent(last)).then(d=>{
const ev=(d.events||[]);if(!ev.length)return;
$("#debrief").hidden=false;
$("#debrief").innerHTML="<b>While you were away:</b> "+esc(ev.slice(-8).map(e=>
e.kind==="fill"?`${e.side} ${e.qty}× ${e.item} @ ${e.px}`:
`${e.state} ${String(e.card_id||"").split(":").slice(0,2).join(" ")}${e.reason?" — "+e.reason:""}`).join(" · "));});}
localStorage.setItem("q_seen",new Date().toISOString());}

function renderStatus(st){
if(!st){$("#status").innerHTML="";return}
const bits=[`<b>${st.scanned}</b> items scanned`];
bits.push(`<b>${st.positions}</b> held`);
if(st.orders)bits.push(`<a id="gotoorders"><b>${st.orders}</b> resting order${st.orders>1?"s":""}</a>`);
bits.push(`<b>${st.entry_cards}</b> new idea${st.entry_cards===1?"":"s"}`);
if(st.deployable_div!=null)bits.push(`<b>${st.deployable_div}</b> div free to deploy`);
let s=bits.join(" · ");
if(!st.entry_cards&&st.entries_reason)s+=`<br>no new buys because: ${esc(st.entries_reason)}`;
$("#status").innerHTML=s;
const go=$("#gotoorders");if(go)go.onclick=()=>{$("#record").open=true;
$("#orders").scrollIntoView({behavior:"smooth",block:"center"})};}

function renderUpdate(u){
u=u||{};const el=$("#updbanner");
if(u.available){el.hidden=false;
el.innerHTML=`<span>A new version is available — <b>${esc(u.current)} → ${esc(u.latest)}</b>. `
+`Your trades and settings are kept.</span>`
+`<button class="small" id="up_go">update &amp; restart</button>`
+`<a id="up_skip">later</a>`;
$("#up_go").onclick=applyUpdate;$("#up_skip").onclick=()=>{el.hidden=true};}
else el.hidden=true;
// engine-room mirror: always shows the running version + check result
const vs=$("#verline");if(vs)vs.textContent="running version "+((D&&D.version)||u.current||"?");
const sx=$("#up_status");if(sx)sx.innerHTML=u.available?`update <b>${esc(u.latest)}</b> ready — use the banner at the top`
:u.err?("check failed: "+esc(u.err)):u.latest?("up to date — "+esc(u.latest)):"not checked yet — click check";}

async function applyUpdate(){
const b=$("#up_go");b.disabled=true;b.textContent="updating…";
const r=await api("/api/update_apply",{});
if(r&&r.ok){toast("installed "+r.version+" — restarting, the page will reconnect");
(function wait(){fetch("/api/state",{headers:hdrs}).then(r=>r.ok?location.reload():setTimeout(wait,1500))
.catch(()=>setTimeout(wait,1500))})();}
else{b.disabled=false;b.textContent="update & restart";toast("update failed: "+((r&&r.err)||"unknown"))}}

function prefillClose(item,qty){if(EDITING)cancelEdit();
$("#f_item").value=item;$("#f_side").value="sell";$("#f_qty").value=qty;$("#f_px").value="";
$("#record").open=true;updateHint();checkMatch();$("#f_item").scrollIntoView({behavior:"smooth",block:"center"});
toast("logging the sale of "+item+" — enter the price you sold at (per unit), then record fill");}
// name matching: mirror the server's snap so the user sees what their entry binds to
function jnorm(s){return (s||"").replace(/’/g,"'").toLowerCase().split(/\s+/).filter(Boolean).join(" ")}
// custom autocomplete (native <datalist> is unreliable): filters D.names, click/Enter to pick
function acFilter(q){const N=(D&&D.names)||[],v=jnorm(q);
if(!v)return N.slice(0,15);
const pre=[],sub=[];for(const n of N){const nn=jnorm(n);if(nn.startsWith(v))pre.push(n);else if(nn.includes(v))sub.push(n);}
return pre.concat(sub).slice(0,15);}
function attachAC(input,onPick){
let box=null,items=[];
function close(){if(box){box.remove();box=null;items=[];}}
function show(){const list=acFilter(input.value);close();if(!list.length)return;
box=document.createElement("div");box.className="acbox";
const r=input.getBoundingClientRect();box.style.left=r.left+"px";box.style.top=(r.bottom+2)+"px";box.style.minWidth=Math.max(r.width,220)+"px";
list.forEach(n=>{const d=document.createElement("div");d.className="acitem";d.textContent=n;
d.onmousedown=e=>{e.preventDefault();input.value=n;close();if(onPick)onPick(n);};box.appendChild(d);});
items=list;document.body.appendChild(box);}
input.addEventListener("input",show);
input.addEventListener("focus",show);
input.addEventListener("keydown",e=>{if(e.key==="Escape")close();
else if(e.key==="Enter"&&items.length){e.preventDefault();input.value=items[0];close();if(onPick)onPick(items[0]);}});
input.addEventListener("blur",()=>setTimeout(close,200));}
async function renameItem(oldName,newName){newName=(newName||"").trim();
if(!newName)return toast("type or pick the item to match it to");
try{const r=await api("/api/rename_item",{old:oldName,new:newName,ledger:D.cfg.mode});
toast(r.moved?`matched "${oldName}" → "${r.item}" — it'll price now`:`no fills moved (is it spelled the same?)`);load();
}catch(e){toast("couldn't match: "+e.message)}}
function checkMatch(){const v=$("#f_item").value,N=(D&&D.names)||[];
if(!v){$("#f_match").textContent="";return}
const hit=N.find(n=>jnorm(n)===jnorm(v));
$("#f_match").innerHTML=hit?`✓ binds to <b>${esc(hit)}</b> — it'll price and match your other trades`
:`<span class="k">not a known item yet — saved exactly as typed (fine for things the scanner can't see)</span>`;}
function orderById(id){return (D.orders||[]).find(o=>String(o.id)===String(id))}
async function orderBought(id,qty){const o=orderById(id);if(!o)return;
const q=num(qty)||o.qty;if(!(q>0))return toast("enter how many actually filled");
try{const r=await api("/api/order_fill",{order_id:+id,qty:q});
toast(`logged buy of ${r.filled}× ${o.item}`+(r.remaining>0?` — ${r.remaining} still resting`:" — it's a holding now"));load();
}catch(e){toast("couldn't record: "+e.message)}}
async function orderSold(id,qty){const o=orderById(id);if(!o)return;
const q=num(qty)||o.qty;if(!(q>0))return toast("enter how many actually filled");
try{const r=await api("/api/order_fill",{order_id:+id,qty:q});
prefillClose(o.item,r.filled);
toast(`logged buy of ${r.filled}× at ${o.px} ex`+(r.remaining>0?` (${r.remaining} still resting)`:"")+" — now enter what you sold it for");
}catch(e){toast("couldn't record: "+e.message)}}
async function discardItem(item){
if(!confirm(`Stop tracking "${item}"? Its recorded fills are voided so it leaves your positions — you still hold it in-game; this just removes it from QUANT.`))return;
try{await api("/api/discard_item",{item,ledger:D.cfg.mode});toast("stopped tracking "+item);load();
}catch(e){toast("couldn't remove: "+e.message)}}
function startEdit(f){EDITING=f;
$("#f_item").value=f.item;$("#f_side").value=f.side;$("#f_qty").value=f.qty;$("#f_px").value=f.px;
$("#f_go").textContent="save edit #"+f.id;$("#f_cancel").hidden=false;$("#fillform").classList.add("editing");
updateHint();checkMatch();$("#record").open=true;$("#f_item").scrollIntoView({behavior:"smooth",block:"center"});
toast("editing trade #"+f.id+" — fix the numbers (price is per unit), then save");}
function cancelEdit(){EDITING=null;$("#f_go").textContent="record fill";$("#f_cancel").hidden=true;
$("#fillform").classList.remove("editing");$("#f_qty").value=$("#f_px").value="";updateHint();}
function updateHint(){const q=num($("#f_qty").value),p=num($("#f_px").value);
const base="Price is per unit (ex), the same \"ex each\" the card shows — not the order total.";
$("#f_hint").innerHTML=(q>0&&p>0)?`${q} × ${p} ex/unit = <b>${(q*p).toFixed(1)} ex total</b>. ${base}`:base;}

async function load(){D=await api("/api/state");render()}
$("#refresh").onclick=async()=>{$("#refresh").textContent="polling…";
try{await api("/api/refresh",{})}catch(e){}
try{await api("/api/update")}catch(e){}      // refresh also re-checks for updates
$("#refresh").textContent="refresh";load();};
$("#up_check").onclick=async()=>{$("#up_status").textContent="checking…";
try{await api("/api/update")}catch(e){toast("check failed: "+e.message)}load();};
$("#trusttog").onclick=()=>{const d=$("#trustdetail");d.hidden=!d.hidden};
$("#cancelall").onclick=async()=>{const n=(D.orders||[]).length;
if(!n||!confirm(`Cancel all ${n} resting order${n>1?"s":""}? (Held positions are not affected.)`))return;
try{const r=await api("/api/cancel_orders",{});toast(`cancelled ${r.cancelled} order${r.cancelled===1?"":"s"} — slots freed`);load();}
catch(e){toast("couldn't cancel: "+e.message)}};
$("#f_go").onclick=async()=>{
const item=$("#f_item").value.trim(),qty=num($("#f_qty").value),px=num($("#f_px").value);
if(!item||!(qty>0)||!(px>0))return toast("need an item, a positive qty, and a positive price PER UNIT");
const body={item,side:$("#f_side").value,qty,px};
try{
if(EDITING){body.id=EDITING.id;body.ledger=EDITING.ledger;
const r=await api("/api/fill_edit",body);toast("trade #"+EDITING.id+" updated → new #"+(r.fill||"?")+", everything recalculated");cancelEdit();}
else{await api("/api/fill",body);toast("fill recorded");$("#f_qty").value=$("#f_px").value="";updateHint();}
load();
}catch(e){toast("couldn't save: "+e.message)}};
$("#f_cancel").onclick=cancelEdit;
$("#f_qty").oninput=updateHint;$("#f_px").oninput=updateHint;$("#f_item").oninput=checkMatch;
attachAC($("#f_item"),checkMatch);
$("#c_go").onclick=async()=>{try{
await api("/api/holdings",{div:num($("#c_div").value)||0,ex:num($("#c_ex").value)||0,chaos:num($("#c_chaos").value)||0});
toast("holdings set — sizing and benchmarks now use your real capital");load();
}catch(e){toast("couldn't save holdings: "+e.message)}};
$("#s_go").onclick=async()=>{await api("/api/mode",{risk:$("#s_risk").value,mode:$("#s_mode").value});
toast("applied");load()};
$("#notif").onclick=()=>Notification.requestPermission().then(p=>toast(p==="granted"?
"exit alerts on — SELL/ABANDON only, entries never notify":"alerts blocked"));
load();setInterval(load,60000);
</script></body></html>"""
