import { useState, useEffect, useCallback } from "react";
import { RefreshCw, Compass, Settings2, Trash2 } from "lucide-react";
import { LineChart, Line, YAxis, ResponsiveContainer } from "recharts";

// QUANT — PoE2 market decision-support dashboard (artifact edition).
// Read-only by design: it watches prices and advises; you trade by hand in-game.
// Persists privately to your account via window.storage. Same play-card schema as quant.py.

const C = {
  bg: "#161310", panel: "#1f1a15", line: "#3a322a", ink: "#e8e0d0",
  dim: "#998f7d", gold: "#c9a86a", up: "#8aa86b", warn: "#c25e4c", info: "#6e93a8",
};
const MONO = { fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace", fontVariantNumeric: "tabular-nums" };
const DISPLAY = { fontFamily: "Georgia, 'Times New Roman', serif", letterSpacing: "0.18em" };
const KEY = "quant:v1";

const DEFAULTS = {
  config: {
    league: "Runes of Aldur",
    start_capital_div: 5,
    no_fill_hours: 48,
    plays: [
      { id: "jewellers", label: "PLACEHOLDER: Perfect Jeweller's hold", match: "Perfect Jeweller's Orb",
        entry_max_ex: 0, exit_target_ex: 0, abandon_drop_pct: 20, budget_div: 1.5 },
      { id: "unique1", label: "PLACEHOLDER: meta unique investment", match: "REPLACE WITH UNIQUE NAME",
        entry_max_ex: 0, exit_target_ex: 0, abandon_drop_pct: 20, budget_div: 2 },
    ],
  },
  snapshots: [],
  fills: [],
};

export default function Quant() {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const [advice, setAdvice] = useState("");
  const [showSettings, setShowSettings] = useState(false);
  const [cfgText, setCfgText] = useState("");
  const [fill, setFill] = useState({ play_id: "", side: "buy", qty: "", price_ex: "", note: "" });

  useEffect(() => {
    (async () => {
      try {
        const r = await window.storage.get(KEY);
        setData(r ? JSON.parse(r.value) : DEFAULTS);
      } catch {
        setData(DEFAULTS);
      }
    })();
  }, []);

  const persist = useCallback(async (next) => {
    setData(next);
    try { await window.storage.set(KEY, JSON.stringify(next)); }
    catch { setErr("Could not save — storage unavailable. Changes are session-only."); }
  }, []);

  if (!data) return (
    <div className="min-h-screen flex items-center justify-center" style={{ background: C.bg, color: C.dim, ...MONO }}>
      loading ledger…
    </div>
  );

  const { config, snapshots, fills } = data;
  const snap = snapshots[snapshots.length - 1] || null;
  const rateNow = snap?.ex_per_div || null;
  const rateStart = snapshots.find((s) => s.ex_per_div)?.ex_per_div || rateNow;

  // ----- portfolio math (mirrors quant.py) -----
  const pos = {};
  let spentEx = 0;
  for (const f of fills) {
    const st = (pos[f.play_id] ||= { qty: 0, cost: 0 });
    if (f.side === "buy") { st.qty += f.qty; st.cost += f.qty * f.price_ex; spentEx += f.qty * f.price_ex; }
    else {
      const avg = st.qty ? st.cost / st.qty : 0;
      st.cost -= f.qty * avg; st.qty -= f.qty; spentEx -= f.qty * f.price_ex;
    }
  }
  const bankroll0 = config.start_capital_div * (rateStart || 0);
  const liquidEx = bankroll0 - spentEx;
  let posEx = 0;
  for (const [pid, st] of Object.entries(pos)) {
    const mkt = snap?.items?.[pid]?.px;
    st.mark = mkt ?? (st.qty ? st.cost / st.qty : 0);
    posEx += st.qty * st.mark;
  }
  const networth = rateNow ? (liquidEx + posEx) / rateNow : null;
  const base = config.start_capital_div;
  const delta = networth != null ? networth - base : null;

  // ----- signals (same semantics as local app) -----
  const signals = [];
  for (const p of config.plays) {
    const d = snap?.items?.[p.id];
    const st = pos[p.id] || { qty: 0 };
    if (!d || d.px == null) { signals.push({ k: "info", t: `${p.label}: no price data — refresh, or fix the match name.` }); continue; }
    const tag = `${p.match} @ ${d.px}ex` + (d.t7 != null ? ` (${d.t7 > 0 ? "+" : ""}${Math.round(d.t7)}% 7d)` : "");
    if (st.qty <= 0 && p.entry_max_ex && d.px <= p.entry_max_ex)
      signals.push({ k: "entry", t: `ENTRY ${p.label}: ${tag} ≤ ceiling ${p.entry_max_ex} — deploy up to ${p.budget_div} div.` });
    else if (st.qty > 0 && p.exit_target_ex && d.px >= p.exit_target_ex)
      signals.push({ k: "exit", t: `EXIT ${p.label}: ${tag} ≥ target ${p.exit_target_ex} — list and sell.` });
    else if (st.qty > 0 && d.t7 != null && d.t7 <= -Math.abs(p.abandon_drop_pct))
      signals.push({ k: "abandon", t: `ABANDON ${p.label}: 7d trend breaches −${p.abandon_drop_pct}% — liquidate at market.` });
    else signals.push({ k: "hold", t: `${p.label}: ${tag} — no trigger.` });
  }
  if (rateStart && rateNow && Math.abs(rateNow - rateStart) / rateStart >= 0.05)
    signals.push({ k: "info", t: `div:ex drift ${(((rateNow - rateStart) / rateStart) * 100).toFixed(0)}% since tracking start — re-check ex-priced targets.` });

  // ----- Claude calls -----
  async function callClaude(content) {
    const res = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model: "claude-sonnet-4-20250514",
        max_tokens: 1000,
        messages: [{ role: "user", content }],
        tools: [{ type: "web_search_20250305", name: "web_search" }],
      }),
    });
    const d = await res.json();
    return (d.content || []).filter((b) => b.type === "text").map((b) => b.text).join("\n");
  }

  async function refreshPrices() {
    setBusy("refresh"); setErr("");
    try {
      const list = config.plays.map((p) => `{"k":"${p.id}","find":"${p.match}"}`).join(",");
      const text = await callClaude(
        `Fetch CURRENT Path of Exile 2 market data for league "${config.league}" using web search ` +
        `(poe.ninja poe2 economy and/or poe2scout). Items: [${list}]. ` +
        `Reply with ONLY minified JSON, no prose, no markdown fences: ` +
        `{"ex_per_div":<exalted orbs per 1 divine orb>,"items":[{"k":"<id>","px":<price in exalted, number or null>,"t7":<7-day % change, number or null>}],"src":"<very short source note>"}`
      );
      const m = text.match(/\{[\s\S]*\}/);
      if (!m) throw new Error("No JSON in reply: " + text.slice(0, 120));
      const j = JSON.parse(m[0].replace(/```json|```/g, ""));
      const items = {};
      for (const it of j.items || []) items[it.k] = { px: it.px, t7: it.t7 ?? null };
      const next = {
        ...data,
        snapshots: [...snapshots, { ts: new Date().toISOString(), ex_per_div: j.ex_per_div ?? null, items, src: j.src }].slice(-150),
      };
      await persist(next);
    } catch (e) { setErr("Refresh failed: " + e.message); }
    setBusy("");
  }

  async function askMove() {
    setBusy("advise"); setErr("");
    try {
      const text = await callClaude(
        `You are "Quant", a terse PoE2 trading advisor. League ${config.league}. ` +
        `Latest snapshot: ${JSON.stringify(snap)}. Rate at start: ${rateStart}. ` +
        `Play rules: ${JSON.stringify(config.plays)}. Open positions (qty, avg cost ex): ` +
        `${JSON.stringify(Object.fromEntries(Object.entries(pos).map(([k, v]) => [k, { qty: v.qty, avg: v.qty ? +(v.cost / v.qty).toFixed(1) : 0 }])))}. ` +
        `Liquid: ${liquidEx.toFixed(0)}ex. Net worth: ${networth?.toFixed(2)} div vs ${base} holding. ` +
        `Give at most 5 prioritized one-line actions for the next 24h. Plain text only. The user trades manually; never suggest automation.`
      );
      setAdvice(text || "(no advice returned)");
    } catch (e) { setErr("Advisor failed: " + e.message); }
    setBusy("");
  }

  async function recordFill() {
    if (!fill.play_id || !fill.qty || !fill.price_ex) { setErr("Fill needs play, qty and price."); return; }
    setErr("");
    await persist({
      ...data,
      fills: [...fills, { ...fill, qty: +fill.qty, price_ex: +fill.price_ex, ts: new Date().toISOString() }],
    });
    setFill({ play_id: "", side: "buy", qty: "", price_ex: "", note: "" });
  }

  async function applySettings() {
    try {
      const j = JSON.parse(cfgText);
      if (!Array.isArray(j.plays)) throw new Error("plays must be an array");
      await persist({ ...data, config: j });
      setShowSettings(false); setErr("");
    } catch (e) { setErr("Settings not applied: " + e.message); }
  }

  async function resetAll() {
    if (!window.confirm("Erase all Quant data (snapshots, fills, settings)?")) return;
    try { await window.storage.delete(KEY); } catch {}
    setData(DEFAULTS); setAdvice("");
  }

  const ageMin = snap ? Math.round((Date.now() - Date.parse(snap.ts)) / 60000) : null;
  const ribbonMax = Math.max(networth ?? base, base) * 1.25;
  const sigColor = { entry: C.up, exit: C.gold, abandon: C.warn, info: C.info, hold: C.line };
  const sparkData = snapshots.filter((s) => s.ex_per_div).map((s) => ({ r: s.ex_per_div }));

  const label = (t) => (
    <span className="text-xs uppercase" style={{ color: C.dim, letterSpacing: "0.12em" }}>{t}</span>
  );
  const input = (props) => (
    <input {...props} className="w-full px-2 py-1 text-sm border" style={{ background: C.bg, borderColor: C.line, color: C.ink, ...MONO }} />
  );

  return (
    <div className="min-h-screen" style={{ background: C.bg, color: C.ink, ...MONO }}>
      {/* header */}
      <div className="flex flex-wrap items-baseline gap-4 px-5 py-3 border-b" style={{ borderColor: C.line }}>
        <h1 className="text-lg m-0" style={{ ...DISPLAY, color: C.gold }}>QUANT</h1>
        {label(config.league)}
        <span>{label("ex / div ")}<b style={{ color: C.gold }}>{rateNow ? Math.round(rateNow) : "—"}</b></span>
        <span>{label("net worth ")}<b style={{ color: C.gold }}>{networth != null ? networth.toFixed(2) : "—"}</b>{label(" div")}</span>
        <span>{label("snapshot ")}<b style={{ color: ageMin > 90 ? C.warn : C.ink }}>{ageMin != null ? ageMin + "m" : "none"}</b></span>
        <div className="flex gap-2 ml-auto">
          <Btn onClick={refreshPrices} disabled={!!busy} icon={<RefreshCw size={14} className={busy === "refresh" ? "animate-spin" : ""} />}>
            {busy === "refresh" ? "Fetching…" : "Refresh prices"}
          </Btn>
          <Btn onClick={() => { setCfgText(JSON.stringify(config, null, 2)); setShowSettings(!showSettings); }} icon={<Settings2 size={14} />}>Settings</Btn>
        </div>
      </div>

      {/* benchmark ribbon — net worth vs "did nothing" */}
      <div className="relative mx-5 mt-4 border" style={{ height: 46, borderColor: C.line, background: C.panel }}>
        <div className="absolute top-0 bottom-0 left-0" style={{ width: `${((networth ?? base) / ribbonMax) * 100}%`, background: `linear-gradient(90deg,#4a3c22,${C.gold})`, opacity: 0.85 }} />
        <div className="absolute" style={{ top: -5, bottom: -5, width: 2, left: `${(base / ribbonMax) * 100}%`, background: C.ink }} />
        <div className="absolute inset-0 flex items-center justify-between px-3 text-xs">
          {label(`vs holding ${base} div`)}
          <b style={{ color: delta == null ? C.dim : delta >= 0 ? C.up : C.warn }}>
            {delta == null ? "—" : `${delta >= 0 ? "+" : ""}${delta.toFixed(2)} div`}
          </b>
        </div>
      </div>

      {err && <div className="mx-5 mt-3 px-3 py-2 text-sm border" style={{ borderColor: C.warn, color: C.warn }}>{err}</div>}

      {showSettings && (
        <Panel title="Settings — same schema as quant.py config.json" className="mx-5 mt-4">
          <textarea value={cfgText} onChange={(e) => setCfgText(e.target.value)} rows={14}
            className="w-full p-2 text-xs border" style={{ background: C.bg, borderColor: C.line, color: C.ink, ...MONO }} />
          <div className="flex gap-2 mt-2">
            <Btn onClick={applySettings}>Apply settings</Btn>
            <Btn onClick={resetAll} icon={<Trash2 size={14} />} danger>Erase all data</Btn>
          </div>
        </Panel>
      )}

      <div className="grid gap-4 px-5 py-4 md:grid-cols-2">
        <div className="flex flex-col gap-4">
          <Panel title="Actions">
            {signals.length === 0 && <span style={{ color: C.dim }}>No data yet — hit Refresh prices.</span>}
            {signals.map((s, i) => (
              <div key={i} className="px-3 py-2 my-1 text-sm" style={{ borderLeft: `3px solid ${sigColor[s.k]}`, background: "rgba(0,0,0,.18)" }}>{s.t}</div>
            ))}
          </Panel>
          <Panel title="Advisor">
            <Btn onClick={askMove} disabled={!!busy || !snap} icon={<Compass size={14} />}>
              {busy === "advise" ? "Thinking…" : "What's my move?"}
            </Btn>
            {!snap && <div className="mt-2 text-xs" style={{ color: C.dim }}>Needs at least one snapshot first.</div>}
            {advice && <pre className="mt-3 text-sm whitespace-pre-wrap" style={{ color: C.ink }}>{advice}</pre>}
          </Panel>
        </div>

        <div className="flex flex-col gap-4">
          <Panel title="Plays">
            <table className="w-full text-sm border-collapse">
              <thead><Tr head cells={["Play", "Price ex", "7d", "Entry≤", "Exit≥", "Qty", "Avg"]} /></thead>
              <tbody>
                {config.plays.map((p) => {
                  const m = snap?.items?.[p.id] || {};
                  const st = pos[p.id] || { qty: 0, cost: 0 };
                  return <Tr key={p.id} cells={[
                    p.label, m.px ?? "—",
                    <span style={{ color: m.t7 > 0 ? C.up : m.t7 < 0 ? C.warn : C.dim }}>{m.t7 != null ? Math.round(m.t7) + "%" : "—"}</span>,
                    p.entry_max_ex || "—", p.exit_target_ex || "—", st.qty || 0,
                    st.qty ? (st.cost / st.qty).toFixed(1) : "—",
                  ]} />;
                })}
              </tbody>
            </table>
            {sparkData.length > 1 && (
              <div style={{ height: 60 }} className="mt-3">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={sparkData}>
                    <YAxis domain={["dataMin", "dataMax"]} hide />
                    <Line dataKey="r" stroke={C.gold} dot={false} strokeWidth={1.5} isAnimationActive={false} />
                  </LineChart>
                </ResponsiveContainer>
                {label("ex/div history")}
              </div>
            )}
          </Panel>

          <Panel title="Holdings & fills">
            <div className="flex gap-6 text-sm mb-3">
              <span>{label("liquid ")}<b>{Math.round(liquidEx)} ex</b></span>
              <span>{label("positions ")}<b>{Math.round(posEx)} ex</b></span>
              <span>{label("fills ")}<b>{fills.length}</b></span>
            </div>
            <div className="grid grid-cols-2 gap-2">
              <select value={fill.play_id} onChange={(e) => setFill({ ...fill, play_id: e.target.value })}
                className="w-full px-2 py-1 text-sm border" style={{ background: C.bg, borderColor: C.line, color: C.ink }}>
                <option value="">— play —</option>
                {config.plays.map((p) => <option key={p.id} value={p.id}>{p.id}</option>)}
              </select>
              <select value={fill.side} onChange={(e) => setFill({ ...fill, side: e.target.value })}
                className="w-full px-2 py-1 text-sm border" style={{ background: C.bg, borderColor: C.line, color: C.ink }}>
                <option value="buy">buy</option><option value="sell">sell</option>
              </select>
              {input({ placeholder: "qty", inputMode: "decimal", value: fill.qty, onChange: (e) => setFill({ ...fill, qty: e.target.value }) })}
              {input({ placeholder: "price in ex", inputMode: "decimal", value: fill.price_ex, onChange: (e) => setFill({ ...fill, price_ex: e.target.value }) })}
              <div className="col-span-2">{input({ placeholder: "note (optional)", value: fill.note, onChange: (e) => setFill({ ...fill, note: e.target.value }) })}</div>
              <div className="col-span-2"><Btn onClick={recordFill} full>Record fill</Btn></div>
            </div>
            <p className="mt-3 text-xs" style={{ color: C.dim }}>
              Fills are how Quant sees your trades — no API exposes your own listings or exchange orders.
              Read-only tool: all trading happens by hand in-game. Refresh and Advisor use your Claude usage.
              Data is stored privately to your account.
            </p>
          </Panel>
        </div>
      </div>
    </div>
  );

  function Panel({ title, children, className = "" }) {
    return (
      <section className={`border p-4 ${className}`} style={{ borderColor: C.line, background: C.panel }}>
        <h2 className="text-xs uppercase mb-3 mt-0 font-normal" style={{ ...DISPLAY, color: C.dim, letterSpacing: "0.2em" }}>{title}</h2>
        {children}
      </section>
    );
  }
  function Btn({ children, icon, onClick, disabled, danger, full }) {
    return (
      <button onClick={onClick} disabled={disabled}
        className={`inline-flex items-center justify-center gap-2 px-3 py-1 text-sm border cursor-pointer ${full ? "w-full" : ""} ${disabled ? "opacity-50" : ""}`}
        style={{ background: "none", borderColor: danger ? C.warn : C.gold, color: danger ? C.warn : C.gold, letterSpacing: "0.06em" }}>
        {icon}{children}
      </button>
    );
  }
  function Tr({ cells, head }) {
    const Cell = head ? "th" : "td";
    return (
      <tr>
        {cells.map((c, i) => (
          <Cell key={i} className={`py-1 px-1 ${i === 0 ? "text-left" : "text-right"} ${head ? "font-normal" : ""}`}
            style={{ borderBottom: `1px solid ${C.line}`, color: head ? C.dim : undefined }}>{c}</Cell>
        ))}
      </tr>
    );
  }
}
