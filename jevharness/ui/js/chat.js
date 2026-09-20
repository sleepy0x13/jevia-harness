// The conversation: how a turn looks, and how a run streams into it.
import {
  $, esc, uid, kb, money, pad2, STAR, S, persist, hooks, env, cat, T, detectLang,
  liveKeys, canJudge, canRun, settingsPayload, md, ask, popMenu, api, toast,
} from "./core.js";
import { openCanvasFor, openAgent, refreshAgent, openDecisions, refreshDecisions } from "./canvas.js";
import { openPrefs } from "./prefs.js";
import { openWorkspace } from "./workspace.js";
import { createRun, reduce, replayAll } from "./run-events.js";
import { runCardParts, patch, stageLabel, startTicker } from "./decision-panel.js";
import { markStatic } from "./motion.js";

export let current = null;
export const setCurrent = (id) => { current = id; };
export const chat = () => S.chats.find((c) => c.id === current) || null;

// One entry per conversation with work in flight: its abort handle and queue.
export const RUNS = new Map();
export const isRunning = (id) => RUNS.has(id);
const queueLength = (id) => (RUNS.get(id) ? RUNS.get(id).queue.length : 0);

let ATT = [];

// Every run's reduced state, by run id. Live runs are fed event by event;
// a recorded one is rebuilt from its saved events, static, on first view.
export const RUNSTATE = new Map();
const MAX_KEPT_EVENTS = 1500;
export function runOf(turn) {
  if (!turn || !turn.run_id) return null;
  let run = RUNSTATE.get(turn.run_id);
  if (!run) {
    run = replayAll(turn.run_id, turn.ev || [], "recorded");
    markStatic(turn.run_id);
    // A record that stops short of its end says how it ended, not "running".
    if (!turn.pending && run.status === "running") run.status = turn.run_status || (turn.error ? "failed" : "completed");
    RUNSTATE.set(turn.run_id, run);
  }
  return run;
}
const newRunId = () => "run-" + uid() + uid();

// The page is rebuilt on every streamed event; entrance motion should play once,
// not every time. Remember what has already been on screen, and in which phase.
const shown = new WeakMap();
function firstTime(obj, phase) {
  const seen = shown.get(obj) || new Set();
  if (seen.has(phase)) return false;
  seen.add(phase); shown.set(obj, seen);
  return true;
}

/* ---------- words for what happened ---------- */
function condText(d, t) {
  if (!d || !d.decision) return (d && d.condition) || "";
  const op = t.ops[d.op] || d.op;
  return `${d.decision} ${op}${d.value != null && ["is", "at_least", "below"].includes(d.op) ? " " + d.value : ""}`;
}
export function stepText(s, t) {
  const fn = t.steps_[s.code];
  if (!fn) return s.detail;
  const d = { ...(s.data || {}) };
  if (s.code === "gate") d.condition_text = condText(s.data, t);
  if (s.code === "compose") d.layout = t.layouts[d.layout] || d.layout;
  try { return fn(d); } catch (e) { return s.detail; }
}
export function logText(entry, t) {
  const fn = t.log[entry.code];
  return fn ? fn(entry) : entry.code;
}

// A decision's question, shortened into something that reads as a label.
const OPENERS = /^(does|do|did|is|are|was|were|has|have|can|could|should|would|will)\s+/i;
function shortLabel(text, name) {
  let s = (text || "").trim();
  if (!s) return String(name || "").replace(/_/g, " ");
  s = s.replace(/[?？]\s*$/, "").replace(/^(the material|this material)\s+/i, "").replace(OPENERS, "");
  s = s.replace(/^(材料中|这段材料|材料里|本文|文中)\s*(是否|有没有)?\s*/, "").replace(/^(是否|有没有|会不会)\s*/, "");
  s = s.replace(/是否(都|会|要)?/g, "").replace(/[吗呢吧]\s*$/, "").trim();
  if (s.length > 46) s = s.slice(0, 46).replace(/[\s，,、]+\S*$/, "") + "…";
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/* ---------- blocks of a turn ---------- */
function gistHTML(st, t) {
  const d = st.decisions || {}, plan = st.plan || {}, unsure = new Set((st.review || {}).uncertain || []);
  const yes = [], no = [], reads = [], shaky = [];
  for (const name of Object.keys(d)) {
    const a = d[name], q = (plan.decisions || {})[name] || {};
    const label = esc(shortLabel(q.instructions, name));
    if (unsure.has(name)) { shaky.push(label); continue; }
    if (a.type === "noul") (a.probability >= 0.5 ? yes : no).push(label);
    else if (a.type === "choice") reads.push(`${label} <b>${esc((q.criteria || {})[a.value] || a.value)}</b>`);
    else if (a.type === "score") reads.push(`${label} <b>${esc((a.legend || {})[String(a.level)] || a.level)}</b>`);
  }
  const line = (cls, key, items) => (items.length
    ? `<div class="g ${cls}"><div class="k">${esc(key)}</div><div class="v">${items.join('<span class="x">/</span>')}</div></div>` : "");
  return `<div class="gist">${line("yes", t.gistYes, yes)}${line("no", t.gistNo, no)}${line("is", t.gistIs, reads)}${line("unsure", t.gistUnsure, shaky)}</div>`;
}

function decHTML(name, a, plan, unsure, t) {
  const q = (plan.decisions || {})[name] || {}, bars = [];
  let big = "";
  if (a.type === "choice") {
    Object.entries(a.probabilities || {}).sort((x, y) => y[1] - x[1]).slice(0, 5).forEach(([k, v]) => bars.push([k, v, k === a.value]));
    big = `${Math.round((a.certainty || 0) * 100)}<small>%</small><span class="w">${esc(a.value)}</span>`;
  } else if (a.type === "score") {
    const lg = a.legend || {};
    Object.entries(a.probabilities || {}).sort((x, y) => Number(x[0]) - Number(y[0]))
      .forEach(([k, v]) => bars.push([lg[k] || k, v, Number(k) === a.level]));
    big = `${a.level}<small>/${Math.max(0, bars.length - 1)}</small><span class="w">${esc(lg[String(a.level)] || "")}</span>`;
  } else {
    bars.push([t.gistYes, a.probability, a.probability >= 0.5], [t.gistNo, 1 - a.probability, a.probability < 0.5]);
    big = `${Math.round(a.probability * 100)}<small>%</small><span class="w">${esc(a.probability >= 0.5 ? t.gistYes : t.gistNo)}</span>`;
  }
  return `<div class="dec"><div><div class="n">${esc(String(name).replace(/_/g, " "))}${unsure ? ` · ${esc(t.unsure)}` : ""}</div>
      <div class="q">${esc(q.instructions || "")}</div></div>
    <div class="big num">${big}</div>
    <div class="bars">${bars.map(([l, v, hi]) => `<div class="b ${hi ? "hi" : ""}"><span class="l" title="${esc(l)}">${esc(l)}</span>
      <span class="t"><span class="f" style="--to:${Math.max(0, Math.min(1, v || 0)).toFixed(4)}"></span></span>
      <span class="p num">${Math.round((v || 0) * 100)}%</span></div>`).join("")}</div></div>`;
}

// What Jev decided, drawn: every judgement is a row with its answer and how
// sure Jev was of it. The full distributions stay one click away.
function judgedHTML(st, t) {
  const names = Object.keys(st.decisions || {});
  if (!names.length) return "";
  const plan = st.plan || {}, unsure = new Set((st.review || {}).uncertain || []);
  const rows = names.map((name) => {
    const a = st.decisions[name], q = (plan.decisions || {})[name] || {};
    let answer, sure;
    if (a.type === "noul") { answer = a.probability >= 0.5 ? t.gistYes : t.gistNo; sure = Math.max(a.probability, 1 - a.probability); }
    else if (a.type === "choice") { answer = (q.criteria || {})[a.value] || a.value; sure = (a.probabilities || {})[a.value] ?? a.certainty; }
    else { answer = (a.legend || {})[String(a.level)] || a.level; sure = a.certainty; }
    return `<div class="jrow ${unsure.has(name) ? "shaky" : ""}">
      <span class="jq">${esc(shortLabel(q.instructions, name))}</span>
      <span class="ja">${esc(answer)}</span>
      <span class="jm"><i style="--to:${Math.max(0, Math.min(1, sure || 0)).toFixed(3)}"></i></span>
      <span class="jp num">${Math.round((sure || 0) * 100)}%</span></div>`;
  }).join("");
  return `<div class="block judged"><div class="bhead"><span class="lbl">${esc(t.judged)}</span>
      <span class="meta">${esc(t.gistCount(names.length, unsure.size))}</span></div>
    <div class="rlegacy">${esc(t.dp.traceUnavailable)}</div>
    <div class="jrows">${rows}</div>
    <details><summary>${esc(t.showAll)}</summary>
      ${names.map((n) => decHTML(n, st.decisions[n], plan, unsure.has(n), t)).join("")}</details></div>`;
}

// How the model for a single-step answer was chosen: Jev's rung, the pick,
// and the cheaper ones that did not clear it.
function routeHTML(st, t) {
  const sel = st.selection;
  if (!sel || sel.jev_only || (st.subtasks || []).length || sel.required == null || st.app) return "";
  const rejected = (sel.cheaper_rejected || []).slice(0, 3);
  const switched = (st.steps || []).filter((s) => s.code === "failover");
  return `<div class="route1">${levelMeter(sel.required)}<span>${esc(t.jevLevel(sel.required))}</span>
    <span class="arrow">→</span><b>${esc(sel.label || "")}</b>
    ${rejected.length ? `<span class="rej">${esc(t.rejected(rejected.join("、")))}</span>` : ""}
    ${switched.map((x) => `<span class="rej">${esc(t.switched((x.data || {}).from, (x.data || {}).to))}</span>`).join("")}</div>`;
}
const levelMeter = (n) => `<span class="lvl" title="${n}/4">${[1, 2, 3, 4].map((k) => `<i class="${k <= n ? "on" : ""}"></i>`).join("")}</span>`;

function traceHTML(turn, t) {
  const rows = turn.trace || [];
  if (!rows.length) return "";
  return `<div class="trace">${rows.map((r) => {
    if (r.stage === "search") return `<div class="r"><span class="k">${esc(t.stSearch)}</span><span>${esc(r.query)}</span></div>`;
    if (r.stage === "picked") return `<div class="r"><span class="k">${esc(t.stPicked)}</span><span><b>${esc(t.picked(r.chosen.length, r.considered))}</b> <span class="d">${esc(r.chosen.map((c) => c.domain).join(" · "))}</span></span></div>`;
    if (r.stage === "fetch") return `<div class="r"><span class="k">${esc(t.stFetch)}</span><span class="d">${r.urls.length}</span></div>`;
    if (r.stage === "filtered") return `<div class="r"><span class="k">${esc(t.stFilter)}</span><span>${esc(t.keptOf(r.kept, r.considered, r.enough))}</span></div>`;
    return "";
  }).join("")}</div>`;
}

// The workers. Each row is live while it runs — what it is doing and the last
// words it wrote — and opens that worker in the side panel, where it can be
// spoken to.
const roleName = (id) => { const r = cat.roles.find((x) => x.id === id); return r ? r.name : id || ""; };
function stepsHTML(st, t, i) {
  const items = st.subtasks || [];
  if (!items.length) return "";
  return `<div class="block workers"><div class="bhead"><span class="lbl">${esc(t.workers)}</span>
      <span class="meta num">${items.filter((x) => x.status === "done").length}/${items.length}</span></div>
    ${items.map((x, k) => {
      const lastLog = (x.log || []).slice(-1)[0];
      const tail = x.status === "running"
        ? (x.output ? x.output.replace(/\s+/g, " ").slice(-90) : lastLog ? logText(lastLog, t) : t.status_.running)
        : x.status === "done" ? `${x.ms ? `${(x.ms / 1000).toFixed(1)}s` : ""}${(x.thread || []).length ? ` · ${t.revisedN((x.thread || []).filter((m) => m.role === "user").length)}` : ""}`
        : t.status_[x.status] || "";
      return `<button class="worker ${esc(x.status || "pending")}" data-agent="${i}:${k}">
        <span class="i num">${x.status === "running" ? '<span class="spin"></span>' : pad2(k + 1)}</span>
        <span class="t"><span class="tt">${esc(x.title)}</span><span class="tl">${esc(tail)}</span></span>
        <span class="w">${x.role ? `<span class="tag">${esc(x.bespoke ? t.bespoke : roleName(x.role))}</span>` : ""}${x.needs_web ? `<span class="tag blue">${esc(t.web)}</span>` : ""}
          ${x.required != null && x.status !== "skipped" ? `${levelMeter(x.required)}<span class="wm">${esc(x.model_label || "")}</span>` : ""}</span>
        <span class="go">→</span></button>`;
    }).join("")}</div>`;
}

// The app being built, laid out like a page being made up: every part Jev
// chose — or every function the model has written so far — is revealed in
// turn, once. When it is done, the board hands over to a blue "Ready." block.
const revealed = new WeakMap();
function freshParts(st, ids) {
  let seen = revealed.get(st);
  if (!seen) { seen = new Set(st.app ? ids : []); revealed.set(st, seen); }
  const fresh = new Set(ids.filter((id) => !seen.has(id)));
  fresh.forEach((id) => seen.add(id));
  return fresh;
}
function composeHTML(st, t, i) {
  const comp = st.composition;
  if (!comp && !st.app_build && !(st.app_modules || []).length) return "";
  const code = !comp || comp.kind === "code";
  const ids = code ? st.app_modules || [] : comp.components;
  const fresh = freshParts(st, ids);
  let wave = 0;
  const part = (id) => {
    const isNew = fresh.has(id), delay = isNew ? wave++ * 140 : 0;
    return `<div class="part p-${code ? "module" : esc(id)} ${isNew ? "in" : ""}" style="--d:${delay}ms">
      <span class="pi num">${pad2(ids.indexOf(id) + 1)}</span><span class="pn">${esc(code ? id : t.components[id] || id)}</span></div>`;
  };
  let board;
  if (code) board = `<div class="board l-modules">${ids.map(part).join("")}</div>`;
  else {
    const side = new Set(comp.side || []);
    const hero = ids.filter((c) => c === "hero").map(part).join("");
    const rest = ids.filter((c) => c !== "hero");
    board = comp.layout === "split" && side.size
      ? `<div class="board l-split">${hero}<div class="col">${rest.filter((c) => !side.has(c)).map(part).join("")}</div><div class="col">${rest.filter((c) => side.has(c)).map(part).join("")}</div></div>`
      : `<div class="board l-${comp.layout === "grid" ? "grid" : "single"}">${hero}${rest.map(part).join("")}</div>`;
  }
  const call = (st.steps || []).find((s) => s.code === "compose");
  const size = st.app ? (st.app.html || "").length : (st.app_build || {}).chars || 0;
  const status = code ? t.forging((size / 1024).toFixed(1), (st.app_build || {}).model || "")
    : t.filling((st.selection || {}).label || "");
  return `<div class="build ${st.app ? "done" : ""}">
    <div class="bhead"><span class="lbl">${esc(t.composed)}</span>
      <span class="meta num">${esc(code ? t.kindCode : t.layouts[comp.layout] || comp.layout)}${call ? ` · ${call.latency_ms} ms` : ""}${code && size ? ` · ${(size / 1024).toFixed(1)} KB` : ""}</span></div>
    ${board}
    ${st.app ? readyHTML(st, t, i) : `<div class="bstatus"><span class="line"></span><span>${esc(status)}</span></div>`}</div>`;
}
function readyHTML(st, t, i) {
  const fresh = firstTime(st.app, "ready") ? "in" : "";
  return `<div class="ready ${fresh}">${STAR}<span class="display">Ready.</span>
    <span class="rt"><b>${esc(st.app.title)}</b><span class="num">app.html · ${((st.app.html || "").length / 1024).toFixed(1)} KB</span></span>
    <button class="btn light" data-app="${i}">${esc(t.run)} ↗</button></div>`;
}
function sourcesHTML(st, t) {
  const ev = st.evidence;
  if (!ev || !(ev.sources || []).length) return "";
  return `<div class="sources"><div class="lbl">${esc(t.sources)}</div>${ev.sources.map((s, i) =>
    `<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer" title="${esc(s.url)}"><b class="num">${pad2(i + 1)}</b><span>${esc(s.title || s.url)}</span></a>`).join("")}</div>`;
}

function ledgerHTML(st, t) {
  const cmp = st.comparison, L = t.dp;
  if (!(st.steps || []).length) return "";
  const cost = (st.ledger || {}).cost || null;
  const known = cost ? cost.known_usd : cmp ? cmp.actual_cost : 0;
  const unknown = cost ? cost.unknown_calls : 0;
  const spent = `${money(known)}${unknown ? ` + ${unknown} ?` : ""}`;
  let h = `<details class="ledger"><summary>${esc(t.details)} · ${esc(spent)}</summary>`;
  h += st.steps.map((s) => `<div class="lrow"><span class="e ${esc(s.engine)}">${esc(s.engine === "harness" ? "—" : s.engine)}</span>
      <span>${esc(stepText(s, t))}</span><span class="c num">${s.latency_ms ? s.latency_ms + " ms" : ""}${s.cost ? "  " + money(s.cost) : ""}</span></div>`).join("");
  if (cmp) {
    // Only what was measured, and only the rows that say something: a cache
    // that was never hit is not a number worth a column.
    const hits = st.cache && st.cache.hits ? Math.round((st.cache.rate || 0) * 100) : 0;
    h += `<div class="meters">
      <div><div class="lbl">${esc(L.knownCost)}</div><div class="v num">${money(known)}</div></div>
      ${unknown ? `<div><div class="lbl">${esc(L.unknownCost)}</div><div class="v num">${unknown}</div></div>` : ""}
      <div><div class="lbl">${esc(L.jevCalls)}</div><div class="v num">${cmp.jev_calls}</div></div>
      <div><div class="lbl">${esc(L.llmCalls)}</div><div class="v num">${cmp.llm_calls}</div></div>
      ${hits ? `<div><div class="lbl">${esc(t.cache)}</div><div class="v num">${hits}<small>%</small></div></div>` : ""}
      <div><div class="lbl">${esc(t.wall)}</div><div class="v num">${((st.elapsed_ms || 0) / 1000).toFixed(1)}<small>s</small></div></div></div>`;
  }
  return h + "</details>";
}

// What the answer did on its way to being written: each tool it called, in
// order. The full record with arguments and results is in the Decisions view.
function toolsHTML(st, t) {
  const calls = st.tools || [];
  if (!calls.length) return "";
  const L = t.dp;
  return `<div class="block workers"><div class="bhead"><span class="lbl">${esc(L.stepsTitle)}</span>
      <span class="meta num">${esc(L.stepsLine((st.turns || []).reduce((n, x) => Math.max(n, x.steps || 0), 1), calls.length))}</span></div>
    <div class="jrows">${calls.map((c) => `<div class="jrow">
      <span class="jq">${esc(c.name)}</span>
      <span class="ja">${esc(argsOf(c.arguments))}</span>
      <span class="jm"></span>
      <span class="jp">${esc(c.allowed ? L.toolStates.ok : L.toolStates.refused)}</span></div>`).join("")}</div></div>`;
}
const argsOf = (args) => {
  const text = Object.entries(args || {}).map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`).join(" ");
  return text.length > 70 ? text.slice(0, 69) + "…" : text;
};

// A streamed reply that turns out to be a web page stops showing its text at
// the point the code starts: the page will run in the panel instead.
const CODE_START = /```\s*html|<!doctype html|<html[\s>]/i;
function visibleText(text) {
  const m = CODE_START.exec(text || "");
  return m ? { text: text.slice(0, m.index).trim(), code: text.length - m.index } : { text: text || "", code: 0 };
}

// A turn is a list of keyed blocks. The page is patched block by block, so a
// streamed token rewrites the body and nothing else — the run card, an open
// detail, a text selection elsewhere in the conversation all stay put.
function turnBlocks(turn, i, n) {
  // Fixed interface words follow the interface language; the task's own
  // language only decides what the models write.
  const t = T();
  const fresh = turn._new && firstTime(turn, "in") ? "fresh" : "";
  if (turn.role === "user") {
    // A long message stays readable: folded after a few lines, open on demand.
    const long = (turn.prompt || "").length > 480 || (turn.prompt || "").split("\n").length > 8;
    const clip = turn.state ? `<div class="clip">◫ ${esc(t.material)} · ${kb(turn.state.length)}</div>` : "";
    return { cls: `turn user ${fresh} ${turn.queued ? "queued" : ""}`, blocks: [["user", `
      <div class="kicker"><span class="n num">${pad2(n + 1)}</span><span>${esc(t.you)}</span>${turn.queued ? `<span>· ${esc(t.waiting)}</span>` : ""}</div>
      <div class="said ${long && !turn.open ? "folded" : ""}">${esc(turn.prompt)}</div>
      ${long ? `<button class="unfold" data-unfold="${i}">${esc(turn.open ? t.collapse : t.expand)}</button>` : ""}${clip}`]] };
  }
  const st = turn.st || {}, sel = st.selection;
  const run = runOf(turn);
  const jevOnly = st.generation_skipped || (sel && sel.jev_only);
  const hasDecisions = Object.keys(st.decisions || {}).length > 0;
  const plan = st.plan ? (t.strategies[st.plan.strategy_code] || "") : "";
  const via = jevOnly ? esc(t.jevOnly) : sel && sel.label ? `${esc(t.via)} <b>${esc(sel.label)}</b>` : "";
  const head = `<div class="via">${[via, plan && `<span class="tag">${esc(plan)}</span>`, st.skip_code ? `<span class="tag">${esc(t.skips[st.skip_code] || "")}</span>` : ""].filter(Boolean).join(" ")}</div>`;

  let body = "";
  if (turn.error) body = `<div class="err-line">${esc(turn.error === "interrupted" ? t.interrupted : turn.error)}</div>`;
  else if (turn.pending && !run && !st.output && !(st.subtasks || []).length && !st.composition && !st.app_build) {
    body = `<div class="working"><span class="bar"></span><span>${esc(stageWord(st, t))}</span></div>`;
  } else if (!(jevOnly && hasDecisions) && !(st.app && /app\.html$/.test((st.output || "").trim()))) {
    const shownText = turn.streaming ? visibleText(st.output).text : st.output || "";
    body = shownText ? (turn.streaming
      ? `<div class="body raw" data-body="${i}">${esc(shownText)}<span class="cursor"></span></div>`
      : `<div class="body" data-body="${i}">${md(shownText)}</div>`) : "";
  }
  const acts = turn.pending ? "" : `<div class="acts">
    ${st.output && !st.app ? `<button class="btn ghost sm" data-copy="${i}">${esc(t.copy)}</button>` : ""}
    <button class="btn ghost sm" data-again="${i}">${esc(t.again)}</button>
    ${turn.saved ? `<span class="saved" title="${esc(turn.saved.chat_abs || "")}">${esc(t.savedTo)} ${esc(turn.saved.chat)}</span>` : ""}</div>`;
  const blocks = [
    ["kicker", `<div class="kicker"><span class="n">${turn.pending ? '<span class="live"></span>' : "JEVia"}</span>${head}</div>`],
    ["trace", traceHTML(turn, t)],
    // One default place for the judgements: the run card when there is a
    // record, the old summary (marked as such) when there is not.
    run ? ["run", null] : ["judged", judgedHTML(st, t)],
    ["route", run ? "" : routeHTML(st, t)],
    ["steps", stepsHTML(st, t, i)],
    ["compose", composeHTML(st, t, i)],
    ["tools", toolsHTML(st, t)],
    ["body", body],
    ["sources", sourcesHTML(st, t)],
    ["ledger", turn.pending ? "" : ledgerHTML(st, t)],
    ["acts", acts],
  ].filter(([, html]) => html === null || html);
  return { cls: `turn bot ${fresh}`, blocks, run };
}

// While nothing has been written yet, say which stage the run is in.
function stageWord(st, t) {
  const last = (st.steps || []).slice(-1)[0];
  if (last) return stepText(last, t);
  if (st.plan) return t.researching;
  if (st.assessed) {
    const skill = st.assessed.skill_id ? t.skillNames[st.assessed.skill_id] || st.assessed.skill : null;
    return t.planning(stepText({ code: "assess", data: { ...st.assessed, skill } }, t));
  }
  return t.thinking;
}

/* ---------- the page ---------- */
function blankHTML() {
  const t = T(), keys = liveKeys().length;
  const ws = S.workspace ? S.workspace.split("/").filter(Boolean).pop() : "";
  const row = (k, v, ok, go) => `<button data-go="${go}"><span>${esc(k)}</span><b class="${ok ? "" : "bad"}">${esc(v)}</b></button>`;
  return `<div class="cover">
    <div class="ctop"><span>JEVia</span><span class="num">${new Date().getFullYear()}</span></div>
    <div class="wordmark">JEVia</div>
    <div class="cbottom">
      ${STAR}
      <div class="cline"><div class="display">${esc(t.heroDisplay)}</div></div>
      <div class="status">
        ${row(t.panelWs, ws || t.missing, !!ws, "ws")}
        ${row(t.panelKeys, keys ? t.nKeys(keys) : env.key ? t.fromEnv : t.missing, keys || env.key, "keys")}
        ${row(t.panelModels, S.models.length ? t.nModels(S.models.length) : t.missing, S.models.length, "models")}
        ${row(t.panelJev, canJudge() ? t.ready : t.missing, canJudge(), "keys")}
      </div>
    </div></div>`;
}

export function render() {
  const c = chat();
  hooks.sidebar();
  const blank = !c || !c.turns.length;
  $("main").classList.toggle("blank", blank);
  const stream = $("stream");
  if (blank) {
    stream.innerHTML = blankHTML();
    stream.querySelectorAll("[data-go]").forEach((el) => (el.onclick = () => {
      if (el.dataset.go === "ws") openWorkspace(S.workspace || "");
      else openPrefs(el.dataset.go);
    }));
    hooks.canvas();
    return;
  }
  let wrap = stream.querySelector(":scope > .wrap");
  if (!wrap || wrap.dataset.chat !== c.id) {
    stream.innerHTML = "";
    wrap = document.createElement("div");
    wrap.className = "wrap";
    wrap.dataset.chat = c.id;
    stream.appendChild(wrap);
  }
  let n = 0;
  const sections = c.turns.map((turn, i) => {
    const view = turnBlocks(turn, i, turn.role === "user" ? n : ++n);
    let sec = wrap.children[i];
    if (!sec || sec._turn !== turn) {
      const fresh = document.createElement("section");
      fresh._turn = turn;
      if (sec) wrap.replaceChild(fresh, sec); else wrap.appendChild(fresh);
      sec = fresh;
    }
    sec.dataset.turn = i;
    if (sec.className !== view.cls) sec.className = view.cls;
    patchBlocks(sec, view.blocks, turn, view.run);
    return sec;
  });
  while (wrap.children.length > sections.length) wrap.lastChild.remove();
  hooks.canvas();
}

function patchBlocks(sec, blocks, turn, run) {
  const keys = new Set(blocks.map(([k]) => k));
  for (const child of [...sec.children]) if (!keys.has(child.dataset.blk)) child.remove();
  let prev = null;
  for (const [key, html] of blocks) {
    let el = sec.querySelector(`:scope > [data-blk="${key}"]`);
    if (!el) { el = document.createElement("div"); el.dataset.blk = key; el._html = null; }
    if (key === "run") paintRunCard(el, turn, run);
    else if (el._html !== html) { el.innerHTML = html; el._html = html; }
    const want = prev ? prev.nextSibling : sec.firstChild;
    if (want !== el) sec.insertBefore(el, want);
    prev = el;
  }
}

function paintRunCard(el, turn, run) {
  el.className = "runcard";
  const legacy = !turn.pending && !(turn.ev || []).length;
  patch(el, runCardParts(run, T().dp, {
    source: run.source, startedAt: turn.pending ? turn.startedAt : null, legacy,
  }));
  if (turn.cancelReq && !run.cancel.requested) {
    const flag = el.querySelector(".rbar .rstage");
    if (flag && !el.querySelector(".rflag")) flag.insertAdjacentHTML("afterend", `<span class="rflag">${esc(T().dp.cancelRequested)}</span>`);
  }
}

// Only the run cards and the open Decisions view — nothing else — on a
// decision event.
function paintRuns() {
  const c = chat();
  if (!c) return;
  document.querySelectorAll("#stream section[data-turn]").forEach((sec) => {
    const turn = c.turns[+sec.dataset.turn];
    const el = sec.querySelector(':scope > [data-blk="run"]');
    if (turn && el) paintRunCard(el, turn, runOf(turn));
  });
  refreshDecisions();
}

// One polite announcement per stage change or finished batch; never per token.
let lastSpoken = "";
function announce(run) {
  const text = stageLabel(run, T().dp);
  if (text === lastSpoken) return;
  lastSpoken = text;
  const live = $("live-status");
  if (live) live.textContent = text;
}

// Follow the newest words only while the reader is at the bottom. Scrolling up
// to read something earlier stops the following; coming back down, or the
// "latest" button, resumes it. Sending or opening a task always jumps down.
let following = true;
export function toBottom(force) {
  const s = $("stream");
  if (force === true) following = true;
  if (following) s.scrollTop = s.scrollHeight;
  paintLatest();
}
function paintLatest() {
  const s = $("stream");
  const away = s.scrollHeight - s.scrollTop - s.clientHeight > 80;
  $("btn-latest").hidden = following || !away;
}

// One listener for everything clickable in the conversation, so patching a
// block never has to re-bind anything.
function onStreamClick(e) {
  const c = chat();
  const b = e.target.closest("[data-copy],[data-again],[data-app],[data-unfold],[data-agent],[data-dp-open],[data-dp-action],[data-dp-answer]");
  if (!b || !c) return;
  if (b.dataset.dpAnswer != null) { answerRun(c, b); return; }
  const sec = b.closest("section[data-turn]");
  const ti = sec ? +sec.dataset.turn : -1;
  if (b.dataset.copy != null) {
    navigator.clipboard.writeText((c.turns[+b.dataset.copy].st || {}).output || "")
      .then(() => { b.textContent = T().copied; }).catch(() => {});
  } else if (b.dataset.again != null) {
    const u = c.turns[+b.dataset.again - 1] || {};
    $("prompt").value = u.prompt || "";
    ATT = u.state ? [{ name: "", text: u.state }] : [];
    renderChips(); grow(); $("prompt").focus();
  } else if (b.dataset.app != null) openCanvasFor(c, +b.dataset.app);
  else if (b.dataset.unfold != null) { const turn = c.turns[+b.dataset.unfold]; turn.open = !turn.open; render(); }
  else if (b.dataset.agent != null) { const [x, y] = b.dataset.agent.split(":").map(Number); openAgent(c, x, y); }
  else if (b.dataset.dpOpen != null) openDecisions(c, ti, b);
  else if (b.dataset.dpAction) runAction(b.dataset.dpAction, c, ti);
}

// The run is blocked on a question. Send what was clicked or typed; the run
// hears it through the same stream it is already writing to.
async function answerRun(c, button) {
  const sec = button.closest("section[data-turn]");
  const turn = sec ? c.turns[+sec.dataset.turn] : null;
  const id = button.dataset.dpAnswer;
  const typed = sec && sec.querySelector(`[data-dp-input="${id}"]`);
  const text = button.dataset.dpText || (typed ? typed.value.trim() : "");
  if (!turn || !turn.run_id || !text) { if (typed) typed.focus(); return; }
  button.disabled = true;
  try {
    await api("/api/runs", { action: "answer", run_id: turn.run_id, question_id: id,
      answer: text, settings: settingsPayload() });
  } catch (err) {
    button.disabled = false;
    toast(String(err.message || err), true);
  }
}

// What a "Needs review" block offers. Each is the user's explicit choice.
export function runAction(action, c, ti) {
  if (action === "add_model") { openPrefs("models"); return; }
  if (action === "allow_downgrade") {
    // Permission for this one run only, and the run is labelled as downgraded.
    const u = c.turns[ti - 1];
    if (!u || isRunning(c.id)) return;
    startTurn(c, u.prompt, u.state || "", [], { allow_downgrade: true });
  }
}

/* ---------- composer ---------- */
export function fail(text) { const e = $("err"); e.hidden = !text; e.textContent = text || ""; }
const material = () => ATT.map((a) => (a.name ? `— ${a.name} —\n${a.text}` : a.text)).join("\n\n");
function renderChips() {
  $("chips").innerHTML = ATT.map((a, i) => `<span class="chip"><span class="nm">${esc(a.name || T().material)}</span>
    <span class="muted num">${kb(a.text.length)}</span><button data-rm="${i}">×</button></span>`).join("");
  $("chips").querySelectorAll("[data-rm]").forEach((b) => (b.onclick = () => { ATT.splice(+b.dataset.rm, 1); renderChips(); }));
}
async function addFiles(files) {
  for (const f of files) {
    if (f.size > 400 * 1024) { fail(`${f.name} > 400 KB`); continue; }
    try { ATT.push({ name: f.name, text: await f.text() }); } catch (e) { fail(String(e.message || e)); }
  }
  renderChips();
}
export function grow() {
  const a = $("prompt");
  // Empty means one line, whatever the layout looked like when this ran.
  if (!a.value) { a.style.height = ""; a.style.overflowY = "hidden"; return; }
  a.style.height = "auto";
  a.style.height = Math.min(a.scrollHeight, 240) + "px";
  a.style.overflowY = a.scrollHeight > 240 ? "auto" : "hidden";
}

export const skillName = (id) => {
  const t = T();
  if (!id) return t.skillAutoShort;
  if (id === "none") return t.skillNone;
  const s = cat.skills.find((x) => x.id === id);
  return t.skillNames[id] || (s ? s.name : id);
};

export function paintComposer() {
  const t = T(), busy = isRunning(current), queued = queueLength(current);
  $("prompt").placeholder = t.promptPlaceholder;
  $("hint").textContent = busy ? (queued ? t.queuedN(queued) : t.willQueue) : "";
  $("hint").classList.toggle("busy", busy);
  $("btn-send").classList.toggle("queue", busy);
  $("btn-stop").hidden = !busy;
  $("btn-stop").title = t.dp.cancelRequested;
  const app = $("btn-app");
  app.classList.toggle("on", !!S.app_forced);
  app.hidden = S.apps_enabled === false;
  app.title = S.app_forced ? t.appModeOn : t.appModeOff;
  $("app-label").textContent = t.appMode;
  $("skill-label").textContent = skillName(S.skill);
  $("btn-skill").classList.toggle("on", !!S.skill);
  renderChips();
}

/* ---------- the slash menu ---------- */
// Type "/" and a word: skills and a couple of commands, filtered as you type,
// chosen with the keyboard or the mouse. The same list opens from the "/" chip.
let slash = { open: false, items: [], at: 0, start: -1 };
function slashItems(q) {
  const t = T();
  const rows = [
    { kind: "skill", id: "", label: t.cmdAuto, key: "auto" },
    ...cat.skills.map((s) => ({ kind: "skill", id: s.id, label: t.skillNames[s.id] || s.name, desc: t.skillDescs[s.id] || s.summary || s.description, key: s.id })),
    { kind: "skill", id: "none", label: t.cmdNone, key: "none" },
    { kind: "app", label: S.app_forced ? t.cmdAppOff : t.cmdApp, key: "app" },
  ];
  q = (q || "").toLowerCase();
  return q ? rows.filter((r) => [r.key, r.label, r.desc, r.id].join(" ").toLowerCase().includes(q)) : rows;
}
function paintSlash() {
  const t = T(), box = $("slash");
  box.hidden = !slash.open;
  if (!slash.open) return;
  box.innerHTML = `<div class="mh"><span class="lbl">${esc(t.slashTitle)}</span><span class="muted">${esc(t.slashHint)}</span></div>`
    + (slash.items.length ? slash.items.map((r, k) => `<button data-k="${k}" class="${k === slash.at ? "at" : ""} ${r.kind === "skill" && r.id === (S.skill || "") ? "on" : ""}">
        <span class="mk"></span><span class="tx"><span>${esc(r.label)}</span>${r.desc ? `<small>${esc(r.desc)}</small>` : ""}</span>
        <code>/${esc(r.key)}</code></button>`).join("") : `<div class="empty">${esc(t.slashEmpty)}</div>`);
  box.querySelectorAll("[data-k]").forEach((b) => {
    b.onmousedown = (e) => { e.preventDefault(); choose(+b.dataset.k); };
    b.onmousemove = () => { if (slash.at !== +b.dataset.k) { slash.at = +b.dataset.k; paintSlash(); } };
  });
  const at = box.querySelector(".at");
  if (at) at.scrollIntoView({ block: "nearest" });
}
function updateSlash() {
  const a = $("prompt"), before = a.value.slice(0, a.selectionStart);
  const m = before.match(/(^|\s)\/([^\s/]*)$/);
  if (!m) { if (slash.open) { slash.open = false; paintSlash(); } return; }
  slash = { open: true, items: slashItems(m[2]), at: 0, start: before.length - m[2].length - 1, typed: true };
  paintSlash();
}
function choose(k) {
  const r = slash.items[k];
  if (!r) return;
  if (slash.typed) {
    const a = $("prompt"), end = a.selectionStart;
    a.value = a.value.slice(0, slash.start) + a.value.slice(end);
    a.setSelectionRange(slash.start, slash.start);
  }
  if (r.kind === "skill") S.skill = r.id;
  else S.app_forced = !S.app_forced;
  persist();
  slash.open = false; paintSlash(); paintComposer(); grow();
  $("prompt").focus();
}
function slashKeys(e) {
  if (!slash.open) return false;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    const n = slash.items.length || 1;
    slash.at = (slash.at + (e.key === "ArrowDown" ? 1 : n - 1)) % n;
    paintSlash(); return true;
  }
  if ((e.key === "Enter" || e.key === "Tab") && slash.items.length) { e.preventDefault(); choose(slash.at); return true; }
  if (e.key === "Escape") { e.preventDefault(); slash.open = false; paintSlash(); return true; }
  return false;
}

/* ---------- sending ---------- */
function ensureChat(title) {
  if (!chat()) {
    const c = { id: uid(), title: title.replace(/\s+/g, " ").slice(0, 46), turns: [], created: Date.now() / 1000 };
    S.chats.unshift(c); current = c.id;
  }
  return chat();
}

export function send() {
  const t = T(), prompt = $("prompt").value.trim();
  if (!prompt) return fail(t.needPrompt);
  if (!canRun()) { fail(t.needKey); openPrefs(liveKeys().length ? "models" : "keys"); return; }
  if (!S.workspace) { fail(t.needWorkspace); openWorkspace(""); return; }
  const state = material();
  const c = ensureChat(prompt);
  $("prompt").value = ""; ATT = []; renderChips(); grow(); fail("");
  if (isRunning(c.id)) {
    // The conversation is busy: hold the message rather than refuse it.
    RUNS.get(c.id).queue.push({ prompt, state });
    c.turns.push({ role: "user", prompt, state, queued: true, _new: true });
    render(); toBottom(true); hooks.paint();
    return;
  }
  startTurn(c, prompt, state);
}

// What the model sees of the conversation so far: finished exchanges only.
function historyOf(c) {
  const out = [];
  for (const turn of c.turns) {
    if (turn.queued || turn.pending) continue;
    if (turn.role === "user") out.push({ role: "user", content: turn.prompt });
    else if (turn.st && turn.st.output && !turn.error) out.push({ role: "assistant", content: turn.st.output });
  }
  return out.slice(-12);
}

async function startTurn(c, prompt, state, queue = [], once = {}) {
  const history = historyOf(c);
  c.turns.push({ role: "user", prompt, state, _new: true });
  const runId = newRunId();
  const turn = { role: "assistant", pending: true, streaming: false, lang: detectLang(prompt), st: { steps: [] },
    run_id: runId, ev: [], startedAt: Date.now(), _new: true };
  RUNSTATE.set(runId, createRun(runId, "live"));
  c.turns.push(turn);
  const idx = c.turns.length - 1;
  const controller = new AbortController();
  RUNS.set(c.id, { controller, turn, queue, runId });
  render(); toBottom(true); hooks.paint(); startTicker();

  // Only touch the page while this conversation is the one on screen.
  const showing = () => current === c.id;
  let frame = 0;
  const refresh = () => {
    if (!showing()) { hooks.sidebar(); return; }
    cancelAnimationFrame(frame);
    frame = requestAnimationFrame(() => { render(); toBottom(); refreshAgent(); refreshDecisions(); keep(); });
  };
  // A run in flight is saved every few seconds, so closing the page mid-run
  // leaves a record that says it was interrupted rather than nothing at all.
  let savedAt = 0;
  const keep = () => {
    if (Date.now() - savedAt < 4000) return;
    savedAt = Date.now();
    try { persist(); } catch (e) { /* over quota: the run still finishes */ }
  };
  let runFrame = 0;
  const refreshRuns = () => {
    if (!showing()) return;
    cancelAnimationFrame(runFrame);
    runFrame = requestAnimationFrame(() => { paintRuns(); toBottom(); });
  };
  const liveRun = RUNSTATE.get(runId);
  const worker = (index) => (turn.st.subtasks || []).find((x) => x.index === index);

  try {
    const res = await fetch("/api/run", {
      method: "POST", headers: { "Content-Type": "application/json" }, signal: controller.signal,
      body: JSON.stringify({ settings: { ...settingsPayload(), ...once }, task: { prompt, state, history },
        run_id: runId, chat: { id: c.id, title: c.title, created: c.created || 0 } }),
    });
    if (!res.ok && (res.headers.get("content-type") || "").includes("json")) {
      const e = await res.json(); throw new Error((e.error && e.error.message) || res.statusText);
    }
    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = "", text = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const frames = buf.split("\n\n"); buf = frames.pop();
      for (const f of frames) {
        const line = f.split("\n").find((l) => l.startsWith("data:"));
        if (!line) continue;
        let ev; try { ev = JSON.parse(line.slice(5).trim()); } catch (e) { continue; }
        const st = turn.st;
        if (ev.type === "decision_event") {
          // The reducer commits the real state now; drawing it is the card's business.
          if (turn.ev.length < MAX_KEPT_EVENTS) turn.ev.push(ev); else turn.ev_truncated = true;
          if (reduce(liveRun, ev).length) { announce(liveRun); refreshRuns(); keep(); }
          continue;
        }
        switch (ev.type) {
          case "plan":
            st.plan = ev.plan;
            if ((ev.plan.steps || []).length && !(st.subtasks || []).length) {
              st.subtasks = ev.plan.steps.map((x, k) => ({ title: (x && x.title) || x, index: k, status: "pending", log: [] }));
            }
            break;
          case "assessed": st.assessed = ev; break;
          case "research": (turn.trace = turn.trace || []).push(ev); break;
          case "evidence": st.evidence = ev.evidence; break;
          case "subtasks": st.subtasks = ev.subtasks; st.routing = ev.routing; break;
          case "subtask": {
            const list = st.subtasks || (st.subtasks = []);
            const at = list.findIndex((x) => x.index === ev.subtask.index);
            const keep = at >= 0 ? list[at] : null;
            const next = { ...ev.subtask };
            // Streamed words arrive before the final record; keep them until it lands.
            if (keep && !next.output && keep.output) next.output = keep.output;
            if (at >= 0) list[at] = next; else list.push(next);
            break;
          }
          case "subtask_delta": { const w = worker(ev.index); if (w) w.output = (w.output || "") + ev.text; break; }
          case "subtask_log": { const w = worker(ev.index); if (w) (w.log = w.log || []).push(ev.entry); break; }
          case "decisions": st.decisions = ev.decisions; st.review = ev.review; break;
          case "selection":
            Object.assign(st, { selection: ev.selection, generation_skipped: ev.generation_skipped, skip_reason: ev.skip_reason });
            break;
          case "composed": st.composition = ev.composition; break;
          case "app_progress": st.app_build = { chars: ev.chars, model: ev.model }; break;
          case "app_module": (st.app_modules = st.app_modules || []).push(ev.name); break;
          case "app":
            st.app = ev.app;
            if (showing()) setTimeout(() => openCanvasFor(c, idx), 450);
            break;
          case "tool":
            (st.tools = st.tools || []).push(ev.tool);
            break;
          case "delta": {
            text += ev.text; st.output = text; turn.streaming = true;
            const seen = visibleText(text);
            if (seen.code) { st.app_build = { chars: seen.code, model: (st.selection || {}).label || "" }; break; }
            const node = showing() ? document.querySelector(`[data-body="${idx}"]`) : null;
            if (node) { node.classList.add("raw"); node.innerHTML = esc(seen.text) + '<span class="cursor"></span>'; toBottom(); continue; }
            break;
          }
          case "retried": st.output = ev.output; text = ev.output; break;
          case "saved": turn.saved = ev; break;
          case "done": {
            const live = st.subtasks || [];
            Object.assign(st, ev.result);
            // A worker's conversation lives only in the browser; keep it.
            (st.subtasks || []).forEach((x) => { const was = live.find((y) => y.index === x.index); if (was && was.thread) x.thread = was.thread; });
            break;
          }
          case "error": throw new Error(ev.message || "failed");
          default: break;
        }
        refresh();
      }
    }
  } catch (e) {
    turn.error = e && e.name === "AbortError" ? T().stoppedListening : String(e.message || e);
  } finally {
    clearTimeout(RUNS.has(c.id) ? RUNS.get(c.id).giveUp : 0);
    turn.pending = false; turn.streaming = false; turn.cancelReq = false;
    if (liveRun.status === "running") liveRun.status = turn.error ? "failed" : "completed";
    turn.run_status = liveRun.status;
    const waiting = RUNS.has(c.id) ? RUNS.get(c.id).queue : [];
    RUNS.delete(c.id);
    const next = waiting.shift();
    const placeholder = next ? c.turns.findIndex((x) => x.queued && x.prompt === next.prompt) : -1;
    if (placeholder >= 0) c.turns.splice(placeholder, 1);
    persist();
    if (showing()) { render(); toBottom(); refreshAgent(); } else hooks.sidebar();
    hooks.paint();
    // On a wide screen the work opens beside the conversation by itself.
    const st = turn.st || {};
    if (showing() && !st.app && innerWidth >= 1100 && ((st.output || "").length > 1500 || (st.evidence && (st.evidence.sources || []).length))) {
      openCanvasFor(c, idx, true);
    }
    if (next) startTurn(c, next.prompt, next.state, waiting);
  }
}

// Stop asks the server to schedule nothing new, and keeps listening until it
// confirms. A request already with a provider may still finish and may still
// be billed; the card says so rather than claiming otherwise.
const GIVE_UP_MS = 20000;
export async function stop() {
  const run = RUNS.get(current);
  if (!run) return;
  // Stopping also drops what was waiting behind it.
  const c = chat();
  if (c) c.turns = c.turns.filter((x) => !x.queued);
  run.queue.length = 0;
  run.turn.cancelReq = true;
  paintRuns(); render();
  let known = false;
  try {
    const res = await api("/api/runs", { action: "cancel", run_id: run.runId, settings: settingsPayload() });
    known = res.status === "cancel_requested";
  } catch (e) { /* an older server: fall back to hanging up */ }
  if (!known) { if (run.controller) run.controller.abort(); return; }
  run.giveUp = setTimeout(() => { if (run.controller) run.controller.abort(); }, GIVE_UP_MS);
}

export function newChat() {
  current = null; ATT = []; renderChips(); fail(""); render(); hooks.paint(); hooks.canvas(); $("prompt").focus();
}

export function wireComposer() {
  $("prompt").addEventListener("input", () => { grow(); updateSlash(); });
  $("stream").addEventListener("click", onStreamClick);
  $("stream").addEventListener("keydown", (e) => {
    if (e.key !== "Enter" || e.shiftKey || !e.target.dataset || !e.target.dataset.dpInput) return;
    e.preventDefault();
    const send = e.target.parentElement.querySelector(`[data-dp-answer="${e.target.dataset.dpInput}"]:last-of-type`);
    if (send) send.click();
  });
  $("stream").addEventListener("scroll", () => {
    const s = $("stream");
    following = s.scrollHeight - s.scrollTop - s.clientHeight < 80;
    paintLatest();
  }, { passive: true });
  $("btn-latest").onclick = () => { const s = $("stream"); s.scrollTo({ top: s.scrollHeight, behavior: "smooth" }); following = true; paintLatest(); };
  addEventListener("resize", grow);
  $("prompt").addEventListener("click", updateSlash);
  $("prompt").addEventListener("blur", () => setTimeout(() => { slash.open = false; paintSlash(); }, 120));
  $("prompt").addEventListener("keydown", (e) => {
    if (slashKeys(e)) return;
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  $("prompt").addEventListener("paste", (e) => {
    const txt = (e.clipboardData || window.clipboardData).getData("text");
    // A large paste is material, not an instruction.
    if (txt && txt.length > 900 && !$("prompt").value.trim()) { e.preventDefault(); ATT.push({ name: "", text: txt }); renderChips(); }
  });
  $("btn-send").onclick = send;
  $("btn-stop").onclick = stop;
  $("btn-plus").onclick = (e) => {
    e.stopPropagation();
    popMenu($("btn-plus"), [
      { label: T().attachFile, run: () => $("file").click() },
      { label: T().attachText, run: async () => { const v = await ask(T().attachText, ""); if (v) { ATT.push({ name: "", text: v }); renderChips(); } } },
    ]);
  };
  $("btn-skill").onmousedown = (e) => e.preventDefault();
  $("btn-skill").onclick = (e) => {
    e.stopPropagation();
    if (slash.open) { slash.open = false; paintSlash(); return; }
    slash = { open: true, items: slashItems(""), at: 0, start: -1, typed: false };
    paintSlash(); $("prompt").focus();
  };
  $("file").onchange = (e) => { addFiles([...e.target.files]); e.target.value = ""; };
  $("btn-app").onclick = () => { S.app_forced = !S.app_forced; persist(); paintComposer(); };
  const box = $("box");
  ["dragenter", "dragover"].forEach((k) => box.addEventListener(k, (e) => { if (e.dataTransfer && [...e.dataTransfer.types].includes("Files")) { e.preventDefault(); box.classList.add("drag"); } }));
  ["dragleave", "drop"].forEach((k) => box.addEventListener(k, () => box.classList.remove("drag")));
  box.addEventListener("drop", (e) => { if (e.dataTransfer && e.dataTransfer.files.length) { e.preventDefault(); addFiles([...e.dataTransfer.files]); } });
}
