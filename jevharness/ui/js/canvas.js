// The right-hand panel: what a conversation produced, apps running live, the
// web pages it read, and every worker — each of which can be spoken to.
import { $, esc, T, md, kb, pad2, hooks, persist, settingsPayload, money, cat, api, toast } from "./core.js";
import { chat, logText, render, runOf, runAction, RUNSTATE } from "./chat.js";
import { decisionsParts, patch, runCardParts } from "./decision-panel.js";
import { createRun, reduce, replayAll } from "./run-events.js";
import { markStatic } from "./motion.js";
import { withPolicy } from "./sandbox.js";

let items = [], viewing = null, device = "desktop";
// Where focus goes back to when the panel closes.
let returnFocus = null;
const grouped = {};
// How much of a long record is on the page: more on request, never all at once.
const view = { batchLimit: 0, expanded: {} };

// Everything worth looking at on its own, newest turn first.
function collect(c) {
  const out = [];
  if (!c) return out;
  c.turns.forEach((turn, i) => {
    const st = turn.st || {};
    if (turn.role !== "assistant") return;
    if (turn.run_id) out.push({ kind: "decisions", turn: i, title: "" });
    if (st.app && st.app.html) out.push({ kind: "app", turn: i, title: st.app.title });
    (st.subtasks || []).forEach((x, k) => out.push({ kind: "agent", turn: i, sub: k, title: x.title }));
    if (!st.app && (st.output || "").length > 400 && !turn.pending) out.push({ kind: "doc", turn: i, title: firstLine(st.output) });
    if (st.evidence && (st.evidence.sources || []).length) out.push({ kind: "web", turn: i, title: "" });
  });
  return out.reverse();
}
const firstLine = (s) => (String(s).split("\n").find((l) => l.trim()) || "").replace(/^#+\s*|\*\*/g, "").slice(0, 60);
const same = (a, b) => a && b && a.kind === b.kind && a.turn === b.turn && a.sub === b.sub;

export function openCanvasFor(c, turnIndex, quiet) {
  items = collect(c);
  const pick = items.find((x) => x.turn === turnIndex && x.kind === "app")
    || (quiet ? items.find((x) => x.turn === turnIndex && (x.kind === "doc" || x.kind === "web")) : null);
  show(pick || null);
}
export function openDecisions(c, turnIndex, trigger) {
  items = collect(c);
  view.batchLimit = 0; view.expanded = {};
  returnFocus = trigger || document.activeElement;
  show(items.find((x) => x.kind === "decisions" && x.turn === turnIndex) || null);
  const first = $("cv-body").querySelector("button, summary, [tabindex]");
  if (first) first.focus({ preventScroll: true });
}

export function openAgent(c, turnIndex, sub) {
  items = collect(c);
  show(items.find((x) => x.kind === "agent" && x.turn === turnIndex && x.sub === sub) || null);
}

const isOpen = () => $("shell").classList.contains("canvas-on");
function setOpen(on) { $("shell").classList.toggle("canvas-on", on); paintToggle(); }

function show(item) {
  viewing = item;
  setOpen(true);
  const t = T();
  $("cv-back").hidden = !item;
  $("cv-tools").hidden = !item || item.kind !== "app";
  if (!item) return showList(t);
  const c = chat(), turn = c && c.turns[item.turn];
  if (!turn) return showList(t);
  const st = turn.st || {};
  if (item.kind === "app") {
    $("cv-title").textContent = st.app.title;
    // No same-origin: the app runs its own scripts but never reaches this
    // page, its storage, or the keys kept in it.
    $("cv-body").innerHTML = `<div class="stage ${device}"><iframe title="${esc(st.app.title)}" sandbox="allow-scripts allow-forms allow-pointer-lock"></iframe></div>`;
    // No same-origin access, and no way to call out: the app runs on what it
    // was built with.
    $("cv-body").querySelector("iframe").srcdoc = withPolicy(st.app.html, location.origin);
    $("cv-device").querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.device === device));
  } else if (item.kind === "doc") {
    $("cv-title").textContent = firstLine(st.output);
    $("cv-body").innerHTML = `<article class="body doc">${md(st.output)}</article>`;
  } else if (item.kind === "decisions") {
    paintDecisions(true);
  } else if (item.kind === "web") {
    $("cv-title").textContent = t.webSources;
    $("cv-body").innerHTML = `<div class="webs">${st.evidence.sources.map((s, n) => {
      let host = s.url;
      try { host = new URL(s.url).hostname.replace(/^www\./, ""); } catch (e) { /* keep the url */ }
      return `<a class="site" href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">
        <span class="i num">${pad2(n + 1)}</span><span class="t"><b>${esc(s.title || host)}</b><small>${esc(host)} · ${esc(t.sectionsKept(s.sections || 0))}</small></span>
        <span class="go">${esc(t.openSite)} ↗</span></a>`;
    }).join("")}</div>`;
  } else {
    paintAgent(true);
  }
}

function showList(t) {
  items = collect(chat());
  $("cv-title").textContent = t.outputs;
  const label = (x) => {
    const st = (chat().turns[x.turn] || {}).st || {};
    if (x.kind === "app") return [x.title, t.appMode];
    if (x.kind === "doc") return [x.title, `${t.doc} · ${kb((st.output || "").length)}`];
    if (x.kind === "web") return [t.webSources, `${(st.evidence.sources || []).length}`];
    if (x.kind === "decisions") {
      const turn = chat().turns[x.turn];
      return [t.dp.decisions, t.dp.status[turn.pending ? "running" : turn.run_status || "completed"] || ""];
    }
    const w = (st.subtasks || [])[x.sub] || {};
    return [x.title, `${t.worker} · ${t.status_[w.status] || ""}${w.model_label ? " · " + w.model_label : ""}`];
  };
  $("cv-body").innerHTML = items.length
    ? `<div class="cvlist">${items.map((x, n) => { const [a, b] = label(x); return `<button class="cv k-${x.kind}" data-cv="${n}">
        <span class="i num">${pad2(n + 1)}</span><span><span class="t">${esc(a)}</span><span class="m">${esc(b)}</span></span><span class="go">→</span></button>`; }).join("")}</div>`
    : `<div class="empty">${esc(t.canvasEmpty)}</div>`;
  $("cv-body").querySelectorAll("[data-cv]").forEach((el) => (el.onclick = () => show(items[+el.dataset.cv])));
}

/* ---------- decisions ---------- */
function paintDecisions(fresh) {
  const c = chat(), turn = c && c.turns[viewing.turn];
  if (!turn) return showList(T());
  const run = runOf(turn);
  const L = T().dp;
  $("cv-title").textContent = L.decisions;
  let host = $("cv-body").querySelector(":scope > .dview");
  if (!host || fresh) { $("cv-body").innerHTML = '<div class="dview"></div>'; host = $("cv-body").firstChild; }
  const titles = Object.fromEntries(((turn.st || {}).subtasks || []).map((x) => [x.id, x.title]));
  patch(host, decisionsParts(run, L, {
    source: turn.pending ? "live" : "recorded", titles, grouped, ...view,
    truncated: !!turn.ev_truncated && !!turn.run_id,
    startedAt: turn.pending ? turn.startedAt : null,
  }));
  if (!run.count && !turn.pending) {
    host.insertAdjacentHTML("beforeend", `<p class="rlegacy">${esc(L.traceUnavailable)}</p>`);
  }
}

export function refreshDecisions() {
  if (viewing && viewing.kind === "decisions" && isOpen()) paintDecisions(false);
  else if (viewing && viewing.kind === "demo" && isOpen()) paintDemo(false);
}

// Clicks inside the panel: grouping, review actions, sync, export, full record.
async function onPanelClick(e) {
  const b = e.target.closest("[data-dp-group],[data-dp-action],[data-demo],[data-demo-speed],[data-demo-replay],[data-dp-more],[data-dp-expand]");
  if (!b) return;
  if (b.dataset.dpMore) { view.batchLimit = (view.batchLimit || 12) + 24; refreshDecisions(); return; }
  if (b.dataset.dpExpand) { view.expanded[b.dataset.dpExpand] = true; refreshDecisions(); return; }
  if (b.dataset.dpGroup) { grouped[b.dataset.dpGroup] = !grouped[b.dataset.dpGroup]; refreshDecisions(); return; }
  if (b.dataset.demo) { playDemo(b.dataset.demo); return; }
  if (b.dataset.demoSpeed) { demo.speed = +b.dataset.demoSpeed; paintDemo(false); return; }
  if (b.dataset.demoReplay != null) { playDemo(demo.name); return; }
  const c = chat(), turn = viewing && c && c.turns[viewing.turn];
  const action = b.dataset.dpAction;
  if (!turn) return;
  if (action === "export") return exportRecord(turn);
  if (action === "load_full") return loadFull(turn);
  if (action === "sync") return syncAnswer(c, turn);
  runAction(action, c, viewing.turn);
}

async function exportRecord(turn) {
  try {
    const res = await fetch("/api/runs", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "export", run_id: turn.run_id, settings: settingsPayload() }) });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error?.message || res.statusText);
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url; a.download = `${turn.run_id}.json`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (e) { toast(String(e.message || e), true); }
}

// A record kept only in the workspace: read it back. No model is called.
async function loadFull(turn) {
  try {
    const data = await api("/api/runs", { action: "get", run_id: turn.run_id, settings: settingsPayload() });
    turn.ev = data.events || [];
    turn.ev_truncated = false;
    RUNSTATE.set(turn.run_id, replayAll(turn.run_id, turn.ev, "recorded"));
    markStatic(turn.run_id);
    paintDecisions(true); render();
  } catch (e) { toast(String(e.message || e), true); }
}

// R02: rewrite the whole from its current parts — the one step a sync needs.
async function syncAnswer(c, turn) {
  const st = turn.st, user = c.turns[viewing.turn - 1] || {};
  const run = runOf(turn);
  const version = ((run.outputs.answer || []).filter((v) => v.version != null).slice(-1)[0] || {}).version || 1;
  try {
    const res = await fetch("/api/sync", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ settings: settingsPayload(), run_id: turn.run_id,
        task: { prompt: user.prompt || "", state: user.state || "" },
        parts: (st.subtasks || []).filter((x) => x.output).map((x) => ({ id: x.id, title: x.title, output: x.output,
          version: x.version || 1, required: x.required, answer_version: version })) }) });
    let text = "";
    await readEvents(res, (ev) => {
      if (ev.type === "decision_event") { feed(turn, ev); return; }
      if (ev.type === "delta") text += ev.text;
      if (ev.type === "error") throw new Error(ev.message);
      if (ev.type === "done") { st.output = ev.output || text; (st.subtasks || []).forEach((x) => { x.stale = false; }); }
    });
  } catch (e) { toast(String(e.message || e), true); }
  persist(); render(); refreshDecisions();
}

function feed(turn, ev) {
  const run = runOf(turn);
  if ((turn.ev || (turn.ev = [])).length < 1500) turn.ev.push(ev);
  reduce(run, ev);
}

async function readEvents(res, onEvent) {
  if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error((e.error && e.error.message) || res.statusText); }
  const reader = res.body.getReader(), dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const frames = buf.split("\n\n"); buf = frames.pop();
    for (const f of frames) {
      const line = f.split("\n").find((l) => l.startsWith("data:"));
      if (line) onEvent(JSON.parse(line.slice(5).trim()));
    }
  }
}

/* ---------- demo replay: fixtures, through the same reducer, always labelled ---------- */
const demo = { name: "", run: null, timer: 0, speed: 1, list: null, card: true };
export async function openDemo(trigger) {
  returnFocus = trigger || document.activeElement;
  viewing = { kind: "demo" };
  setOpen(true);
  $("cv-back").hidden = false; $("cv-tools").hidden = true;
  if (!demo.list) {
    try { demo.list = (await (await fetch("/ui/fixtures/index.json")).json()).fixtures || []; } catch (e) { demo.list = []; }
  }
  paintDemo(true);
}
async function playDemo(name) {
  clearTimeout(demo.timer);
  const meta = demo.list.find((x) => x.name === name);
  if (!meta) return;
  const data = await (await fetch(`/ui/fixtures/${encodeURIComponent(meta.file)}`)).json();
  const events = data.events || [];
  const runId = `demo-${name}-${Date.now().toString(36)}`;
  demo.name = name;
  demo.run = createRun(runId, "demo");
  demo.meta = meta;
  paintDemo(true);
  // Replay pacing is only presentation: the recorded elapsed times shown are
  // the recorded ones, whatever the speed.
  let k = 0;
  const step = () => {
    if (viewing?.kind !== "demo" || demo.run.run_id !== runId) return;
    const ev = { ...events[k], run_id: runId };
    reduce(demo.run, ev);
    paintDemo(false);
    k++;
    if (k < events.length) {
      const gap = Math.min(900, Math.max(60, ((events[k].elapsed_ms || 0) - (events[k - 1].elapsed_ms || 0)))) / demo.speed;
      demo.timer = setTimeout(step, gap);
    }
  };
  step();
}
function paintDemo(fresh) {
  const L = T().dp;
  $("cv-title").textContent = L.demoTitle;
  if (fresh) $("cv-body").innerHTML = '<div class="demo"><div class="dctl"></div><div class="dcard runcard"></div><div class="dview"></div></div>';
  const box = $("cv-body").querySelector(".demo");
  if (!box) return;
  box.querySelector(".dctl").innerHTML = `<span class="badge demo">${esc(L.sources_.demo)}</span>
    <span class="lbl">${esc(L.demoPick)}</span>
    <div class="dlist">${(demo.list || []).map((x) => `<button class="btn sm ${x.name === demo.name ? "on" : ""}" data-demo="${esc(x.name)}">${esc(x.title)}</button>`).join("")}</div>
    ${demo.run ? `<div class="dspeed"><button class="btn ghost sm" data-demo-replay="1">${esc(L.replay)}</button>
      <span class="lbl">${esc(L.speed)}</span>${[1, 2, 4].map((v) => `<button class="btn ghost sm ${demo.speed === v ? "on" : ""}" data-demo-speed="${v}">${v}×</button>`).join("")}</div>` : ""}
    ${demo.run ? "" : `<p class="note">${esc(L.demoNote)}</p>`}`;
  if (!demo.run) return;
  patch(box.querySelector(".dcard"), runCardParts(demo.run, L, { source: "demo", noButton: true }));
  patch(box.querySelector(".dview"), decisionsParts(demo.run, L, { source: "demo", grouped, ...view }));
}

/* ---------- a worker ---------- */
const roleText = (w, t) => {
  if (w.bespoke) return t.bespoke;
  const role = cat.roles.find((r) => r.id === w.role);
  return role ? role.name : w.role || "";
};
function paintAgent(fresh) {
  const c = chat(), turn = c && c.turns[viewing.turn], st = (turn && turn.st) || {};
  const w = (st.subtasks || [])[viewing.sub];
  if (!w) return showList(T());
  const t = T();
  $("cv-title").textContent = w.title;
  const busy = w.status === "running" || w.talking;
  const thread = w.thread || [];
  const html = `<div class="agent">
    <div class="ahead"><span class="tag ink">${esc(roleText(w, t))}</span>
      <span class="tag ${w.status === "done" ? "blue" : w.status === "running" ? "acid" : ""}">${esc(t.status_[w.status] || w.status || "")}</span>
      <span class="muted num">${esc(w.model_label || "")}${w.ms ? ` · ${(w.ms / 1000).toFixed(1)}s` : ""}${w.cost ? ` · ${money(w.cost)}` : ""}</span></div>
    ${w.persona ? `<details class="persona"><summary>${esc(t.brief)}</summary><p>${esc(w.persona)}</p></details>` : ""}
    <div class="alog">${(w.log || []).map((e, n) => `<div class="r"><span class="i num">${pad2(n + 1)}</span><span>${esc(logText(e, t))}</span></div>`).join("")
      || `<div class="r"><span class="i num">—</span><span>${esc(t.status_[w.status] || "")}</span></div>`}</div>
    <div class="aout ${busy ? "busy" : ""}" id="agent-out">${w.talking && w.draft != null
      ? `<div class="body raw">${esc(w.draft)}<span class="cursor"></span></div>`
      : w.status === "running" ? `<div class="body raw">${esc(w.output || "")}<span class="cursor"></span></div>`
        : `<div class="body">${md(w.output || "")}</div>`}</div>
    ${thread.length ? `<div class="athread">${thread.map((m) => `<div class="m ${m.role}"><span class="lbl">${esc(m.role === "user" ? t.you : t.worker)}</span>
      <span>${esc(m.role === "user" ? m.content : t.revisedNote)}</span></div>`).join("")}</div>` : ""}
    ${w.stale ? `<div class="banner">${esc(t.answerNotSynced)}</div>` : ""}
    <div class="asay"><textarea class="input" id="agent-say" rows="2" placeholder="${esc(t.talkTo)}" ${busy || turn.pending ? "disabled" : ""}></textarea>
      <button class="send" id="agent-send" ${busy || turn.pending ? "disabled" : ""}>↑</button></div>
  </div>`;
  const keep = !fresh && document.getElementById("agent-say") ? document.getElementById("agent-say").value : "";
  // A repaint must not move the reader: stay where they were, and keep
  // following the words only if they were already at the bottom.
  const view = $("cv-body");
  const top = view.scrollTop, atEnd = view.scrollHeight - view.scrollTop - view.clientHeight < 80;
  view.innerHTML = html;
  view.scrollTop = fresh ? 0 : busy && atEnd ? view.scrollHeight : top;
  const say = $("agent-say");
  say.value = keep;
  say.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); talk(); } });
  $("agent-send").onclick = talk;
}

export function refreshAgent() {
  if (viewing && viewing.kind === "agent" && isOpen()) paintAgent(false);
  else if (isOpen() && !viewing) showList(T());
  paintToggle();
}

// Speak to one worker. It rewrites its own part; the answer is spliced back
// into the whole when the whole was a plain join of the parts.
async function talk() {
  const c = chat(), turn = c.turns[viewing.turn], st = turn.st, w = st.subtasks[viewing.sub];
  const message = $("agent-say").value.trim();
  if (!message || w.talking) return;
  const user = c.turns[viewing.turn - 1] || {};
  w.talking = true; w.draft = ""; w.log = [...(w.log || [])];
  const thread = w.thread || [];
  paintAgent(true);
  try {
    const res = await fetch("/api/subagent", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        settings: settingsPayload(), message,
        task: { prompt: user.prompt || "", state: user.state || "" },
        step: w, thread, outline: st.subtasks.map((x) => x.title),
        others: st.subtasks.filter((x) => x !== w && x.output).map((x) => [x.title, x.output]),
        run_id: turn.run_id,
        answer_mode: (st.steps || []).some((s) => s.code === "assemble") ? "rewritten" : "",
      }),
    });
    if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error((e.error && e.error.message) || res.statusText); }
    const reader = res.body.getReader(), dec = new TextDecoder();
    let buf = "", frame = 0;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const frames = buf.split("\n\n"); buf = frames.pop();
      for (const f of frames) {
        const line = f.split("\n").find((l) => l.startsWith("data:"));
        if (!line) continue;
        const ev = JSON.parse(line.slice(5).trim());
        if (ev.type === "decision_event") { if (turn.run_id) feed(turn, ev); continue; }
        if (ev.type === "log") w.log.push(ev.entry);
        else if (ev.type === "delta") w.draft += ev.text;
        else if (ev.type === "error") throw new Error(ev.message);
        else if (ev.type === "done") {
          w.thread = [...thread, { role: "user", content: message }, { role: "assistant", content: ev.output }];
          w.output = ev.output; w.sources = ev.sources || w.sources;
          w.cost = (w.cost || 0) + (ev.cost || 0);
          if (ev.version) w.version = ev.version;
          resplice(st, w);
        }
        cancelAnimationFrame(frame);
        frame = requestAnimationFrame(() => paintAgent(false));
      }
    }
  } catch (e) {
    w.log.push({ code: "error", message: String(e.message || e) });
  } finally {
    w.talking = false; w.draft = null;
    persist(); paintAgent(true); render();
  }
}

// When the answer was the parts joined under headings, the new part replaces
// the old one in place. A rewritten whole cannot be patched honestly, so it is
// left alone and marked.
function resplice(st, w) {
  const joined = (st.steps || []).some((s) => s.code === "assemble_free");
  const single = (st.subtasks || []).filter((x) => x.output).length === 1;
  if (single) { st.output = w.output; return; }
  if (!joined) { w.stale = true; return; }
  st.output = st.subtasks.filter((x) => x.output)
    .map((x) => `## ${x.title.replace(/^#+\s*/, "").trim()}\n\n${x.output.replace(/^#{1,6}\s.*\n+/, "")}`).join("\n\n");
  w.stale = false;
}

function download() {
  const c = chat(), st = viewing && c && (c.turns[viewing.turn] || {}).st;
  if (!st || !st.app) return;
  const url = URL.createObjectURL(new Blob([st.app.html], { type: "text/html" }));
  const a = document.createElement("a");
  a.href = url; a.download = (st.app.title || "app").replace(/[\\/:*?"<>|]+/g, "-") + ".html";
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function paintToggle() {
  const c = chat(), n = collect(c).length;
  $("btn-canvas").hidden = !c || !c.turns.length || isOpen();
  $("canvas-count").textContent = n;
  $("main").classList.toggle("has-out", !$("btn-canvas").hidden);
}

export function refreshCanvas() {
  if (isOpen() && viewing && viewing.kind === "demo") { paintToggle(); return; }
  if (isOpen() && viewing) {
    items = collect(chat());
    if (!items.some((x) => same(x, viewing))) show(null);
  } else if (isOpen()) showList(T());
  paintToggle();
}

function closePanel() {
  clearTimeout(demo.timer);
  setOpen(false);
  if (returnFocus && document.contains(returnFocus)) returnFocus.focus({ preventScroll: true });
  returnFocus = null;
}

export function wireCanvas() {
  hooks.canvas = refreshCanvas;
  hooks.demo = openDemo;
  $("cv-back").onclick = () => { clearTimeout(demo.timer); show(null); };
  $("cv-close").onclick = closePanel;
  $("cv-body").addEventListener("click", onPanelClick);
  // Escape closes the panel and puts focus back where it was opened from.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !isOpen() || e.defaultPrevented) return;
    const inPrompt = document.activeElement === $("prompt");
    if (inPrompt && $("prompt").value) return;
    closePanel();
  });
  $("cv-download").onclick = download;
  $("btn-canvas").onclick = () => show(null);
  $("cv-device").querySelectorAll("button").forEach((b) => (b.onclick = () => {
    device = b.dataset.device;
    const stage = $("cv-body").querySelector(".stage");
    if (stage) stage.className = "stage " + device;
    $("cv-device").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
  }));
}

// Switching conversations leaves the last one's work behind.
export function closeCanvas() { viewing = null; setOpen(false); }
