// State, persistence, the server, and the small things every module needs.
import { COPY } from "./copy.js";

export const $ = (id) => document.getElementById(id);
export const esc = (t) => String(t == null ? "" : t)
  .replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export const uid = () => Math.random().toString(36).slice(2, 10);
export const kb = (n) => (n < 1024 ? n + " B" : (n / 1024).toFixed(n < 10240 ? 1 : 0) + " KB");
export const money = (v) => (!v ? "$0" : v < 1e-6 ? "<$0.000001" : "$" + v.toFixed(v < 0.01 ? 6 : 4));
export const pad2 = (n) => String(n).padStart(2, "0");
// The one graphic in the system: a thin radial asterisk.
export const STAR = '<svg class="star" viewBox="0 0 100 100" aria-hidden="true"><path d="M50 3v94M3 50h94M16.8 16.8l66.4 66.4M83.2 16.8L16.8 83.2" fill="none" stroke="currentColor" stroke-width="3"/></svg>';

export const STANCES = {
  thrifty: { decision_abstain_below: 0.45, noul_uncertain_low: 0.42, noul_uncertain_high: 0.58, capability_round_up_below: 0.35 },
  balanced: { decision_abstain_below: 0.55, noul_uncertain_low: 0.35, noul_uncertain_high: 0.65, capability_round_up_below: 0.5 },
  careful: { decision_abstain_below: 0.7, noul_uncertain_low: 0.25, noul_uncertain_high: 0.75, capability_round_up_below: 0.7 },
};

// The two cheap models every new install starts with; both run on an
// OpenRouter key, and each can be swapped out in Settings.
const STARTER_MODELS = [
  { id: "m1", model: "deepseek/deepseek-v4-flash", capability: 2, price_in: 0.037, price_out: 0.073, label: "DeepSeek V4 Flash" },
  { id: "m2", model: "qwen/qwen3.7-flash", capability: 3, price_in: 0.03, price_out: 0.13, label: "Qwen3.7 Flash" },
];

const DEFAULTS = {
  lang: "en", keys: [], models: STARTER_MODELS, jev_model: "",
  stance: "balanced", thresholds: { ...STANCES.balanced },
  escalate_uncertain: true, guard_output: true, research_enabled: true, tools_enabled: true,
  memory_enabled: true, apps_enabled: true, app_forced: false,
  motion: "system", allow_downgrade: false, routing_extra: false,
  tools: "write", agent_loop: true, max_steps: 8, approve_changes: false, plan_mode: false,
  quality: "thrift",
  skill: "", workspace: "", onboarded: false,
  chats: [], folders: [], showArchived: false, sideWidth: 252, canvasWidth: 440,
};
const STORE = "jevia.v5";
const LEGACY = "jevia.v4";
const clone = (x) => JSON.parse(JSON.stringify(x));

function load() {
  const base = clone(DEFAULTS);
  try {
    const raw = localStorage.getItem(STORE);
    if (raw) return Object.assign(base, JSON.parse(raw));
    // One key per install became one key per provider: carry the old one over
    // rather than making anyone paste it again.
    const old = localStorage.getItem(LEGACY);
    if (old) {
      const o = JSON.parse(old);
      const keys = (o.key || "").trim() ? [{ ref: "k1", vendor: "openrouter", api_key: o.key.trim(), label: "OpenRouter" }] : [];
      return Object.assign(base, {
        lang: o.lang || "en", keys,
        models: (o.models || STARTER_MODELS).map((m) => ({ ...m, credential_ref: keys.length ? "k1" : "default" })),
        stance: o.stance || "balanced", thresholds: o.thresholds || base.thresholds,
        escalate_uncertain: o.escalate_uncertain !== false, research_enabled: o.research_enabled !== false,
        memory_enabled: o.memory_enabled !== false, skill: o.skill || "", workspace: o.workspace || "",
        onboarded: !!o.onboarded, chats: o.chats || [], folders: o.folders || [],
        sideWidth: o.sideWidth || 252,
      });
    }
  } catch (e) { /* a corrupt store is a fresh start, not a crash */ }
  return base;
}

export const S = load();
// A run that was in flight when the page closed cannot be resumed: the stream
// is gone. Say so, rather than leaving workers waiting for ever.
for (const chat of S.chats || []) {
  for (const turn of chat.turns || []) {
    if (turn.role !== "assistant" || !turn.pending) continue;
    turn.pending = false;
    turn.streaming = false;
    turn.run_status = turn.run_status || "interrupted";
    if (!(turn.st && turn.st.output)) turn.error = turn.error || "interrupted";
    for (const step of (turn.st && turn.st.subtasks) || []) {
      if (step.status === "running" || step.status === "pending") step.status = "failed";
    }
  }
}
// Every model names the key it runs on; the starter ones use the first key, or
// the server's own when there is none.
S.models.forEach((m) => { if (!m.credential_ref) m.credential_ref = (S.keys[0] && S.keys[0].ref) || "default"; });

export function persist() {
  const slim = (keepApps) => S.chats.slice(0, 80).map((c, i) => ({
    id: c.id, title: c.title, folder: c.folder || null, archived: !!c.archived, created: c.created || 0,
    turns: c.turns.filter((turn) => !turn.queued).map((turn) => {
      if (turn.role === "user") return { role: "user", prompt: turn.prompt, state: turn.state };
      const st = { ...(turn.st || {}) };
      if (st.app && !(keepApps || i < 8)) st.app = { ...st.app, html: "" };
      // Decision events travel with recent chats; older ones keep only the
      // run id, and their record is read back from the workspace on demand.
      const keepEv = keepApps && i < 8 && turn.ev;
      return { role: "assistant", lang: turn.lang, st, error: turn.error, trace: turn.trace, saved: turn.saved,
        // Kept so a run cut short by closing the page is restored as
        // interrupted, not as finished.
        pending: !!turn.pending,
        run_id: turn.run_id, run_status: turn.run_status, ev: keepEv ? turn.ev : undefined,
        ev_truncated: keepEv ? turn.ev_truncated : !!(turn.ev && turn.ev.length) };
    }),
  }));
  for (const keepApps of [true, false]) {
    try {
      localStorage.setItem(STORE, JSON.stringify({ ...S, chats: slim(keepApps) }));
      return;
    } catch (e) { /* over quota: drop older apps' pages and try once more */ }
  }
}

// Modules register their renderers here so none has to import the others.
export const hooks = { render() {}, paint() {}, sidebar() {}, canvas() {}, demo() {} };
export const env = { key: false, jevModel: "~typesafe/jev-latest" };
export const cat = { vendors: [], skills: [], roles: [] };

// Fixed interface words follow the interface language only. A task written in
// Chinese under an English interface still gets English labels; the model's
// own output stays in the language it was written in.
export const T = () => COPY[S.lang] || COPY.en;

// Mirrors the server's rule: a CJK character carries about half a word, and a
// mostly-English sentence with a Chinese clause is still English.
export function detectLang(text) {
  const s = String(text || "");
  const cjk = (s.match(/[\u4e00-\u9fff\u3040-\u30ff]/g) || []).length;
  const words = (s.match(/[A-Za-z]+/g) || []).length;
  if (cjk && cjk / 2 >= words) return "zh";
  if (words) return "en";
  return cjk ? "zh" : S.lang;
}

export const vendorOf = (id) => cat.vendors.find((v) => v.id === id) || { id, name: id, chats: true, serves_jev: false };
export const liveKeys = () => S.keys.filter((k) => (k.api_key || "").trim());
export const jevKey = () => liveKeys().find((k) => vendorOf(k.vendor).serves_jev);
export const canJudge = () => !!jevKey() || env.key;
export const canChat = () => liveKeys().some((k) => vendorOf(k.vendor).chats) || env.key;
export const canRun = () => canJudge() && canChat() && S.models.some((m) => m.model);

export function settingsPayload() {
  const creds = liveKeys().map((k) => ({
    ref: k.ref, vendor: k.vendor, api_key: k.api_key.trim(), base_url: k.base_url || "", label: k.label || "",
  }));
  const refs = new Set(creds.map((c) => c.ref));
  const jev = jevKey();
  return {
    language: S.lang,
    jev_model: S.jev_model || "",
    jev_credential_ref: jev ? jev.ref : "default",
    roster: {
      credentials: creds,
      models: S.models.filter((m) => m.model).map((m) => ({
        id: m.id, model: m.model, label: m.label || m.model, capability: m.capability,
        // A missing price stays missing: the server treats it as unknown, not free.
        price_in: m.price_in ?? null, price_out: m.price_out ?? null,
        priced: m.priced !== false && m.price_in != null && m.price_out != null,
        // A model whose key was removed falls back to the server's own key.
        credential_ref: refs.has(m.credential_ref) || (m.credential_ref === "default" && env.key)
          ? m.credential_ref : (creds[0] ? creds[0].ref : "default"),
        enabled: true,
      })),
    },
    thresholds: S.thresholds,
    escalate_uncertain: !!S.escalate_uncertain, guard_output: !!S.guard_output,
    research_enabled: S.research_enabled !== false, tools_enabled: S.tools_enabled !== false,
    memory_enabled: S.memory_enabled !== false,
    app_mode: S.apps_enabled === false ? "never" : S.app_forced ? "always" : "auto",
    allow_downgrade: !!S.allow_downgrade, routing_extra: !!S.routing_extra,
    tools: S.tools || "write", agent_loop: S.agent_loop !== false,
    max_steps: S.max_steps || 8,
    approve_changes: !!S.approve_changes, plan_mode: !!S.plan_mode,
    quality: S.quality || "thrift",
    skill: S.skill || null, workspace: S.workspace || null,
  };
}

export async function api(path, body) {
  const res = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch (e) { /* not json */ }
  if (!res.ok) throw new Error((data.error && data.error.message) || res.statusText);
  return data;
}

/* ---------- markdown: escaped first, so no model text becomes markup ---------- */
export function md(src) {
  const lines = esc(src || "").split("\n");
  const out = [];
  let list = null, para = [], code = false, buf = [];
  const flushP = () => { if (para.length) { out.push("<p>" + inline(para.join(" ")) + "</p>"); para = []; } };
  const flushL = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const raw of lines) {
    const line = raw.replace(/\s+$/, "");
    if (/^\s*```/.test(line)) {
      if (code) { out.push("<pre><code>" + buf.join("\n") + "</code></pre>"); buf = []; code = false; }
      else { flushP(); flushL(); code = true; }
      continue;
    }
    if (code) { buf.push(line); continue; }
    if (!line.trim()) { flushP(); flushL(); continue; }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { flushP(); flushL(); const lv = Math.min(3, Math.max(2, h[1].length)); out.push(`<h${lv}>${inline(h[2])}</h${lv}>`); continue; }
    if (/^\s*([-*_])\1{2,}\s*$/.test(line)) { flushP(); flushL(); out.push("<hr>"); continue; }
    const q = line.match(/^&gt;\s?(.*)$/);
    if (q) { flushP(); flushL(); out.push("<blockquote>" + inline(q[1]) + "</blockquote>"); continue; }
    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ul || ol) {
      const want = ul ? "ul" : "ol";
      flushP();
      if (list !== want) { flushL(); out.push(`<${want}>`); list = want; }
      out.push("<li>" + inline((ul || ol)[1]) + "</li>");
      continue;
    }
    flushL(); para.push(line.trim());
  }
  if (code && buf.length) out.push("<pre><code>" + buf.join("\n") + "</code></pre>");
  flushP(); flushL();
  return out.join("");
}
function inline(t) {
  return t
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/\[(\d{1,2})\]/g, '<sup class="cite">$1</sup>');
}

/* ---------- in-app dialogs: a sandboxed page never shows the browser's own ---------- */
let asking = null, toastTimer = null;
export function ask(title, value, { confirm = false, danger = false } = {}) {
  return new Promise((resolve) => {
    asking = resolve;
    $("ask-title").textContent = title;
    const input = $("ask-input");
    input.hidden = confirm; input.value = value || "";
    $("ask-ok").textContent = confirm ? T().del : T().save;
    $("ask-ok").classList.toggle("danger", danger);
    $("ask").classList.add("on"); $("askscrim").classList.add("on");
    setTimeout(() => (confirm ? $("ask-ok") : input).focus(), 40);
  });
}
function answer(value) {
  $("ask").classList.remove("on"); $("askscrim").classList.remove("on");
  const fn = asking; asking = null; if (fn) fn(value);
}
export function wireAsk() {
  $("ask-ok").onclick = () => answer($("ask-input").hidden ? true : $("ask-input").value.trim());
  $("ask-cancel").onclick = () => answer(null);
  $("askscrim").onclick = () => answer(null);
  $("ask").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); $("ask-ok").click(); }
    if (e.key === "Escape") { e.preventDefault(); answer(null); }
  });
}
export function toast(text, bad = false) {
  $("toast-text").textContent = text;
  $("toast").classList.toggle("bad", bad);
  $("toast").classList.add("on");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("toast").classList.remove("on"), 2600);
}
export function popMenu(anchor, rows) {
  const menu = $("popmenu");
  menu.innerHTML = rows.map((r, i) => (r === "-" ? "<hr>"
    : `<button data-i="${i}" class="${r.danger ? "danger" : ""} ${r.on ? "on" : ""}"><span class="mk"></span>
        <span class="tx"><span>${esc(r.label)}</span>${r.desc ? `<small>${esc(r.desc)}</small>` : ""}</span></button>`)).join("");
  menu.querySelectorAll("[data-i]").forEach((b) => (b.onclick = (e) => { e.stopPropagation(); closeMenu(); rows[+b.dataset.i].run(); }));
  menu.hidden = false;
  const r = anchor.getBoundingClientRect();
  menu.style.minWidth = Math.max(200, r.width) + "px";
  const h = menu.offsetHeight, w = menu.offsetWidth;
  menu.style.left = Math.max(8, Math.min(r.left, innerWidth - w - 8)) + "px";
  menu.style.top = (r.bottom + h + 8 > innerHeight ? Math.max(8, r.top - h - 6) : r.bottom + 6) + "px";
  anchor.classList.add("open");
  menu.dataset.owner = anchor.id || "";
  openAnchor = anchor;
}
let openAnchor = null;

// Every choice-from-a-list in the product is this: a button that reads like a
// field, opening the same menu as everything else. No native <select>.
export function dropdown(el, items, value, onPick) {
  const current = items.find((x) => x.value === value) || items[0];
  el.classList.add("dd");
  el.type = "button";
  el.textContent = current ? current.label : "";
  el.onclick = (e) => {
    e.stopPropagation();
    if (openAnchor === el && !$("popmenu").hidden) { closeMenu(); return; }
    popMenu(el, items.map((x) => ({ label: x.label, desc: x.desc, on: x.value === value, run: () => onPick(x.value) })));
  };
}

export const closeMenu = () => {
  $("popmenu").hidden = true;
  if (openAnchor) openAnchor.classList.remove("open");
  openAnchor = null;
};
