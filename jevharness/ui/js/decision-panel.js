// What Jev decided, what the rules did with it, and which calls did not happen.
//
// Two surfaces, one source: a compact run card at the top of every answer
// (status bar, the batch in flight, a short decision summary) and the full
// Decisions view in the side panel. Both are built from the reducer's state
// only — never from DOM, never from invented numbers — and updated by key,
// so a streamed token does not rebuild them and an open detail stays open.
//
// The HTML builders are pure (tested in node); `patch` is the only DOM code.
import { STAGE_KEYS, GROUPS, groupItems, tally, generationClaim, policiesFor, choiceDimensions, summaryItems, openQuestions } from "./run-events.js";
import { claim, MAX_ANIMATED } from "./motion.js";

export const esc = (t) => String(t == null ? "" : t)
  .replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const n2 = (v) => (v == null || Number.isNaN(+v) ? null : (+v).toFixed(2));
const clamp = (v) => Math.max(0, Math.min(1, +v || 0));
const STAR = '<svg class="star" viewBox="0 0 100 100" aria-hidden="true"><path d="M50 3v94M3 50h94M16.8 16.8l66.4 66.4M83.2 16.8L16.8 83.2" fill="none" stroke="currentColor" stroke-width="4"/></svg>';
const money = (v) => (v == null ? null : v === 0 ? "$0" : v < 1e-6 ? "<$0.000001" : "$" + (+v).toFixed(v < 0.01 ? 6 : 4));
const secs = (ms) => `${((ms || 0) / 1000).toFixed(1)}s`;

/* ---------- words ---------- */
export const stageLabel = (run, L) => {
  if (run.status && run.status !== "running" && run.status !== "cancel_requested") return L.status[run.status] || run.status;
  return L.stages[STAGE_KEYS[run.stage] || "running"] || L.stages.running;
};
const actorLabel = (a, L) => (a ? L.actors[a] || a : "");
const reasonLabel = (r, L) => L.reasons[r] || r;
const purposeLabel = (p, L) => L.purposes[p] || p || "";

export function ruleText(row, L) {
  const base = String(row.rule || "").replace(/\/v\d+$/, "");
  const fn = L.rules[base];
  const action = (L.actions && L.actions[row.action]) || row.action || "";
  try { if (fn) return fn(row.inputs || {}, action); } catch (e) { /* fall through */ }
  return action || base;
}

/* ---------- the three answer types (M02) ---------- */
// Noul: P(yes), and the threshold with its direction. Never "No 2%".
function noulHTML(r, L, extra = {}) {
  const p = r.probability_yes;
  if (p == null) return `<span class="unav">${esc(L.unavailable)}</span>`;
  const t = extra.threshold;
  return `<span class="pv"><span class="pk">${esc(L.pYes)}</span> <b class="num">${n2(p)}</b></span>
    <span class="pbar" role="img" aria-label="${esc(L.pYes)} ${n2(p)}${t != null ? `, ${esc(L.threshold)} ${n2(t)}` : ""}">
      <i class="fill" style="--to:${clamp(p).toFixed(3)}"></i>${t != null ? `<i class="tick" style="left:${(clamp(t) * 100).toFixed(1)}%"></i>` : ""}</span>
    ${t != null ? `<span class="thr num">${esc(L.threshold)} ${n2(t)}</span>` : ""}`;
}
// Choice: the chosen option with its own probability, and Confidence apart.
function choiceHTML(r, q, L, open) {
  const probs = r.probabilities;
  const label = (k) => (q && q.options && q.options[k]) || k;
  if (!probs) return `<span class="pv"><b>${esc(label(r.choice))}</b></span> <span class="unav">${esc(L.distribution)}: ${esc(L.unavailable)}</span>`;
  const rows = Object.entries(probs).sort((a, b) => b[1] - a[1]);
  const top = rows.slice(0, 4);
  const bar = ([k, v]) => `<div class="crow ${k === r.choice ? "on" : ""}"><span class="cl">${esc(label(k))}</span>
      <span class="pbar"><i class="fill" style="--to:${clamp(v).toFixed(3)}"></i></span><span class="num">${n2(v)}</span></div>`;
  const sum = rows.reduce((s, [, v]) => s + v, 0);
  return `<div class="choice"><div class="pv"><b>${esc(label(r.choice))}</b> <span class="num">${esc(L.pOf)} ${n2(probs[r.choice])}</span></div>
    <div class="conf"><span class="pk">${esc(L.confidence)}</span> <b class="num">${r.confidence == null ? esc(L.unavailable) : n2(r.confidence)}</b></div>
    <div class="crows">${top.map(bar).join("")}</div>
    ${rows.length > 4 ? `<details class="all" ${open ? "open" : ""}><summary>${esc(L.viewAll)} (${rows.length})</summary>${rows.slice(4).map(bar).join("")}
      <div class="sum num">${esc(L.total)} ${n2(sum)}</div></details>` : ""}</div>`;
}
// Score: the question's own scale, the raw value, and the adopted integer apart.
function scoreHTML(r, q, L, adopted) {
  const levels = (q && q.levels) || Object.values(r.legend || {});
  const n = Math.max(levels.length, 2);
  if (r.score == null) return `<span class="unav">${esc(L.unavailable)}</span>`;
  const at = clamp(r.score / (n - 1));
  return `<div class="score"><div class="pv"><span class="pk">${esc(L.rawScore)}</span> <b class="num">${n2(r.score)}</b>
      <span class="muted num">${esc(L.onScale(0, n - 1))}</span>
      ${adopted != null ? `<span class="adopt">${esc(L.adopted)} <b class="num">${esc(adopted)}</b></span>` : ""}</div>
    <div class="scale" role="img" aria-label="${esc(L.rawScore)} ${n2(r.score)}">${Array.from({ length: n }, (_, k) =>
      `<i class="lv ${adopted != null && k === +adopted ? "adopted" : ""}" style="left:${(k / (n - 1) * 100).toFixed(1)}%" title="${esc(levels[k] || k)}"></i>`).join("")}
      <i class="mark" style="left:${(at * 100).toFixed(1)}%"></i></div>
    <div class="conf"><span class="pk">${esc(L.confidence)}</span> <b class="num">${r.confidence == null ? esc(L.unavailable) : n2(r.confidence)}</b></div></div>`;
}

export function resultHTML(r, q, L, extra = {}) {
  if (!r) return `<span class="unav">${esc(L.missing)}</span>`;
  if (r.malformed) return `<span class="unav">${esc(L.unavailable)}</span>`;
  if (r.type === "noul") return noulHTML(r, L, extra);
  if (r.type === "choice") return choiceHTML(r, q, L, extra.open);
  if (r.type === "score") return scoreHTML(r, q, L, extra.adopted);
  return `<span class="unav">${esc(L.unavailable)}</span>`;
}

/* ---------- the batch in flight (M01) ---------- */
function thresholdFor(run, b, qid) {
  const pol = policiesFor(run, b.batch_id, qid).find((p) => p.inputs && p.inputs.threshold != null);
  if (pol) return pol.inputs.threshold;
  const rev = run.reviews.find((x) => x.batch_id === b.batch_id && x.question_id === qid && x.threshold);
  return rev && rev.threshold && rev.threshold.high != null ? rev.threshold.high : null;
}
function adoptedFor(run, b, qid, q) {
  // A step's capability question: the policy's adopted level for that step.
  if (/^c\d+$/.test(qid || "")) {
    const route = Object.values(run.routing).find((x) => x.selected && x.selected.batch_id === b.batch_id
      && q && x.selected.title === q.target);
    return route ? route.selected.adopted : null;
  }
  if (qid === "__capability__") {
    const route = run.routing._answer;
    return route && route.selected ? route.selected.adopted : null;
  }
  return null;
}

// Cells: neutral while the request is out, then all settle together. No cell
// is pre-filled, and a failed batch never turns into "No".
export function batchStripHTML(run, b, L) {
  const qs = b.questions.length ? b.questions : Object.keys(b.results).map((id) => ({ question_id: id }));
  const shown = qs.slice(0, 12);
  const phase = b.status === "pending" ? "enter" : "settle";
  const anim = claim(run.run_id, `strip:${b.batch_id}`, phase);
  const cell = (q) => {
    const r = b.results[q.question_id];
    const name = L.qnames[q.question_id] || q.target || q.label || q.question_id;
    let value;
    if (b.status === "pending") value = `<span class="wait" aria-hidden="true"></span>`;
    else if (b.status === "failed") value = `<span class="bad">${esc(L.failed)}</span>`;
    else if (!r) value = `<span class="unav">${esc(L.missing)}</span>`;
    else if (r.type === "noul") value = r.probability_yes == null ? `<span class="unav">${esc(L.unavailable)}</span>` : `<span class="num">${esc(L.pYesShort)} ${n2(r.probability_yes)}</span>`;
    else if (r.type === "choice") value = `<b>${esc((q.options && q.options[r.choice]) || r.choice)}</b>`;
    else if (r.type === "score") value = r.score == null ? `<span class="unav">${esc(L.unavailable)}</span>` : `<span class="num">${n2(r.score)}</span>`;
    else value = `<span class="unav">${esc(L.unavailable)}</span>`;
    return `<div class="cell ${esc(b.status)}"><span class="cn" title="${esc(q.label || "")}">${esc(name)}</span><span class="cv">${value}</span></div>`;
  };
  return `<div class="strip ${esc(b.status)} ${anim}" data-batch="${esc(b.batch_id)}">
    <div class="sh"><span class="lbl">${esc(purposeLabel(b.purpose, L))}</span>
      <span class="meta num">${esc(L.oneCall(qs.length))}${b.status === "failed" ? ` · ${esc(L.failed)}: ${esc(b.reason || "")}` : ""}</span></div>
    <div class="cells">${shown.map(cell).join("")}${qs.length > shown.length ? `<div class="cell cmore"><span class="cn num">+${qs.length - shown.length}</span></div>` : ""}</div></div>`;
}

/* ---------- the run card: status bar + batch + summary ---------- */
export function statusBarHTML(run, L, opts = {}) {
  const live = run.status === "running" || run.status === "cancel_requested";
  const actor = live ? (openActor(run) || run.actor) : null;
  const elapsed = run.completed || !live ? run.elapsed_ms : null;
  return `<div class="rbar ${live ? "running" : "ended"} st-${esc(run.status)}">
    <span class="rmark">${STAR}</span>
    <span class="rstage" data-stagekey="${esc(run.stage || run.status)}">${esc(stageLabel(run, L))}</span>
    ${actor ? `<span class="ractor a-${esc(actor)}">${esc(actorLabel(actor, L))}</span>` : ""}
    ${run.cancel.requested && !run.cancel.confirmed ? `<span class="rflag">${esc(L.cancelRequested)}</span>` : ""}
    ${run.cancel.confirmed && run.cancel.may_bill ? `<span class="rflag">${esc(L.mayBill)}</span>` : ""}
    <span class="rtime num" ${live && opts.startedAt ? `data-rtime="${opts.startedAt}"` : ""}>${elapsed != null ? secs(elapsed) : ""}</span>
    ${opts.source && opts.source !== "live" ? `<span class="rsrc">${esc(L.sources_[opts.source] || opts.source)}</span>` : ""}
    ${opts.noButton ? "" : `<button class="rview" data-dp-open="1">${esc(L.viewDecisions)} →</button>`}</div>`;
}
function openActor(run) {
  if (run.batchOrder.some((id) => run.batches[id].status === "pending")) return "jev";
  if (run.llm.started > run.llm.completed + run.llm.failed) return "llm";
  return null;
}

function summaryRow(item, run, L) {
  const tag = (text, cls = "") => `<span class="tag ${cls}">${esc(text)}</span>`;
  switch (item.kind) {
    case "assess":
      return [L.sum.assess, item.rows.map((r) => esc(ruleText(r, L))).join(" · ")];
    case "evidence": {
      const last = item.e.rounds[item.e.rounds.length - 1];
      const c = (last && last.counts) || {};
      const chk = item.e.checks.filter((x) => x.scope !== "delivery").slice(-1)[0];
      return [L.sum.evidence, `${esc(L.groupsCount(c))}${chk ? " " + tag(L.sufficiency[chk.result] || chk.result, chk.result === "sufficient" ? "blue" : "warn") : ""}`];
    }
    case "routing": {
      const sel = item.rows.filter((r) => r.selected).length, un = item.rows.filter((r) => r.unavailable).length;
      return [L.sum.routing, `${esc(L.selectedN(sel))}${un ? " " + tag(`${L.noQualified} × ${un}`, "bad") : ""}`];
    }
    case "decisions": {
      const b = item.batch, q = (b.questions || []).length;
      const unsure = run.reviews.filter((x) => x.batch_id === b.batch_id && x.reason === "low_confidence").length;
      return [L.sum.decisions, `${esc(L.oneCall(q))}${b.status === "failed" ? " " + tag(L.reasons.jev_failed, "bad") : ""}${unsure ? " " + tag(`${L.reasons.low_confidence} × ${unsure}`, "warn") : ""}`];
    }
    case "plan": {
      const plan = item.plan;
      return [L.planTitle, plan.decided === false ? esc(L.planChanged)
        : plan.decided ? esc(L.planApproved) : esc(L.planWaiting)];
    }
    case "steps": {
      const failed = item.steps.filter((s) => s.tools.some((call) => call.status === "failed" || !call.allowed)).length;
      return [L.stepsTitle, `${esc(L.stepsLine(item.steps.length, item.tools))}`
        + (failed ? ` <span class="tag warn">${esc(L.toolsFailed(failed))}</span>` : "")
        + (item.compactions.length ? ` <span class="tag">${esc(L.compacted(item.compactions.length))}</span>` : "")];
    }
    case "skipped":
      return [L.generationSkipped, `${esc(item.gen.kind === "zero" ? L.zeroLLM : L.noGenCallStep)}`];
    case "review":
      return [L.needsReview, [...new Set(item.rows.map((r) => r.reason))].map((r) => tag(reasonLabel(r, L), r === "low_confidence" ? "warn" : "bad")).join(" ")];
    default: return ["", ""];
  }
}

export function summaryHTML(run, L, open = true) {
  const items = summaryItems(run);
  if (!items.length) return "";
  const tl = tally(run);
  return `<details class="rsum" ${open ? "open" : ""}><summary><span class="lbl">${esc(L.decisionSummary)}</span>
      <span class="meta num">${esc(L.countsLine(tl.judgements, tl.jevCalls, tl.llmCalls))}</span></summary>
    ${items.map((it) => { const [k, v] = summaryRow(it, run, L); return `<div class="ritem" data-sum="${esc(it.key)}"><span class="k">${esc(k)}</span><span class="v">${v}</span></div>`; }).join("")}</details>`;
}

// The card's parts, keyed, so `patch` can replace only what changed.
export function runCardParts(run, L, opts = {}) {
  const pending = run.batchOrder.map((id) => run.batches[id]).filter((b) => b.status === "pending");
  const lastDone = run.batchOrder.map((id) => run.batches[id]).filter((b) => b.status !== "pending").slice(-1)[0];
  const strip = pending[pending.length - 1] || (run.status === "running" ? lastDone : null);
  const parts = [["bar", statusBarHTML(run, L, opts)]];
  if (opts.legacy) parts.push(["legacy", `<div class="rlegacy">${esc(L.traceUnavailable)}</div>`]);
  if (run.gaps.length) parts.push(["gap", `<div class="rgap">${esc(L.recordGap(run.gaps))}</div>`]);
  if (strip) parts.push(["strip", batchStripHTML(run, strip, L)]);
  const asking = askHTML(run, L);
  if (asking) parts.push(["ask", asking]);
  const block = reviewBlockHTML(run, L);
  if (block) parts.push(["block", block]);
  parts.push(["sum", summaryHTML(run, L, opts.summaryOpen !== false)]);
  return parts;
}

// A run that stopped to ask. It is the only thing on the card that wants the
// reader's hands, so it sits above everything and says what is waiting.
export function askHTML(run, L) {
  const open = openQuestions(run);
  if (!open.length) return "";
  return open.map((q) => oneQuestion(run, q, L, open.length)).join("");
}

function oneQuestion(run, q, L, total) {
  const anim = claim(run.run_id, `question:${q.id}`, "enter");
  const label = q.kind === "approval" ? L.approvalNeeded : q.kind === "plan" ? L.planReady : L.questionAsked;
  const options = (q.options || []).map((option, i) =>
    `<button class="btn ${i === 0 ? "fill" : ""} sm" data-dp-answer="${esc(q.id)}" data-dp-text="${esc(option)}">${esc(option)}</button>`).join("");
  return `<div class="rask ${anim}" role="group" aria-label="${esc(label)}">
    <div class="rk"><b>${esc(label)}</b>${q.kind === "approval" ? `<span class="muted">${esc(L.approvalNote)}</span>` : ""}
      ${total > 1 ? `<span class="muted">${esc(L.ofOpen(total))}</span>` : ""}</div>
    <pre class="rq">${esc(q.text)}</pre>
    <div class="racts">${options}
      <input class="input sm" id="dp-answer-${esc(q.id)}" placeholder="${esc(q.kind === "plan" ? L.planChange : L.answerHere)}"
        data-dp-input="${esc(q.id)}">
      <button class="btn sm" data-dp-answer="${esc(q.id)}">${esc(L.send)}</button></div></div>`;
}

// "No qualified model" asks for a decision; the other review states inform.
function reviewBlockHTML(run, L) {
  const blocked = run.reviews.filter((r) => r.reason === "no_qualified_model");
  if (!blocked.length || run.status === "running") return "";
  const r = blocked[0];
  // Worded here from the numbers, so it follows the interface language.
  const route = run.routing[r.subtask_id || "_answer"];
  const u = route && route.unavailable;
  const detail = u ? `${L.requiredLevel} ${u.required_capability} · ${L.bestAvailable(u.best_available)}` : r.detail || "";
  return `<div class="rblock" role="group" aria-label="${esc(L.noQualified)}"><b>${esc(L.noQualified)}</b>
    <span>${esc(detail)}</span>
    <span class="acts"><button class="btn sm" data-dp-action="add_model">${esc(L.addModel)}</button>
    <button class="btn sm" data-dp-action="allow_downgrade">${esc(L.allowDowngrade)}</button></span></div>`;
}

/* ---------- the Decisions view ---------- */
export const BATCHES_SHOWN = 12;
export const QUESTIONS_SHOWN = 24;
function techHTML(pairs, L) {
  const rows = pairs.filter(([, v]) => v != null && v !== "");
  if (!rows.length) return "";
  return `<details class="tech"><summary>${esc(L.technical)}</summary><dl>${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd class="num">${esc(v)}</dd>`).join("")}</dl></details>`;
}

function questionHTML(run, b, q, L, opts) {
  const r = b.results[q.question_id];
  const thr = thresholdFor(run, b, q.question_id);
  const adopted = adoptedFor(run, b, q.question_id, q);
  const pols = policiesFor(run, b.batch_id, q.question_id).filter((p) => !p.question_id || p.question_id === q.question_id);
  const revs = run.reviews.filter((x) => x.batch_id === b.batch_id && x.question_id === q.question_id);
  const why = [...pols.map((p) => `<li><b>${esc(ruleText(p, L))}</b> <code>${esc(p.rule || "")}</code></li>`),
    ...revs.map((x) => `<li><b>${esc(reasonLabel(x.reason, L))}</b> ${esc(x.detail || "")}</li>`)];
  let value;
  if (b.status === "pending") value = `<span class="wait">${esc(L.waitingJev)}</span>`;
  else if (b.status === "failed") value = `<span class="bad">${esc(L.reasons.jev_failed)}: ${esc(b.reason || "")}</span>`;
  else value = resultHTML(r, q, L, { threshold: thr, adopted });
  return `<div class="q ${revs.length ? "flag" : ""}" data-q="${esc(q.question_id)}">
    <div class="qh"><span class="qt">${esc(L.types[q.type] || q.type || "")}</span>
      <span class="qn">${esc(L.qnames[q.question_id] || q.target || q.question_id)}</span>
      ${revs.map((x) => `<span class="tag ${x.reason === "low_confidence" ? "warn" : "bad"}">${esc(reasonLabel(x.reason, L))}</span>`).join("")}</div>
    <div class="ql">${esc(q.label || "")}</div>
    <div class="qv">${value}</div>
    ${why.length ? `<details class="why"><summary>${esc(L.whyThis)}</summary><ul>${why.join("")}</ul></details>` : ""}
    ${techHTML([[L.tech.question, q.question_id], [L.tech.batch, b.batch_id], [L.tech.attempt, b.attempt_id]], L)}</div>`;
}

export function batchCardHTML(run, b, L, opts = {}) {
  const u = b.usage || {};
  const dims = b.status === "completed" ? choiceDimensions(b) : [];
  const group = dims.length && opts.grouped && opts.grouped[b.batch_id];
  const anim = claim(run.run_id, `card:${b.batch_id}`, b.status === "pending" ? "enter" : "settle");
  const all = b.questions.length ? b.questions : Object.keys(b.results).map((id) => ({ question_id: id, type: b.results[id].type }));
  // Long batches list the first questions and count the rest; nothing is dropped.
  const open = opts.expanded && opts.expanded[b.batch_id];
  const qs = open ? all : all.slice(0, QUESTIONS_SHOWN);
  const hiddenQs = all.length - qs.length;
  return `<section class="bcard ${esc(b.status)} ${anim}" data-key="batch:${esc(b.batch_id)}">
    <div class="bh"><span class="lbl">${esc(purposeLabel(b.purpose, L))}</span>
      <span class="meta num">${esc(L.oneCall(all.length))}${b.ended_ms != null && b.started_ms != null ? ` · ${secs(b.ended_ms - b.started_ms)}` : ""}</span>
      <span class="st">${esc(b.status === "pending" ? L.waitingJev : b.status === "failed" ? L.failed : L.settled)}</span></div>
    ${dims.length ? `<div class="gtoggle"><button class="btn ghost sm" data-dp-group="${esc(b.batch_id)}">${esc(group ? L.listView : L.groupView)}</button></div>` : ""}
    ${group ? groupedHTML(b, dims[0], L) : `<div class="qs">${qs.map((q) => questionHTML(run, b, q, L, opts)).join("")}</div>
      ${hiddenQs ? `<button class="btn ghost sm" data-dp-expand="${esc(b.batch_id)}">${esc(L.showAllQuestions(all.length))}</button>` : ""}`}
    ${techHTML([[L.tech.call, u.call_id], [L.tech.costSource, u.cost_source ? L.costSources[u.cost_source] || u.cost_source : null],
      [L.tech.cost, u.cost_usd != null ? money(u.cost_usd) : L.costSources.unknown], [L.tech.operation, b.operation_id], [L.tech.stage, b.stage]], L)}</section>`;
}

// M02 grouping: items by one Choice dimension; flags stay on the cards.
// Changing the view re-reads the same results; it never calls anything.
function groupedHTML(b, dim, L) {
  const options = Object.keys(dim[0].options || {});
  const cols = Object.fromEntries(options.map((o) => [o, []]));
  const unassigned = [];
  for (const q of dim) {
    const r = b.results[q.question_id];
    if (r && r.choice in cols) cols[r.choice].push([q, r]); else unassigned.push([q, r]);
  }
  const flagsFor = (q) => (b.questions || []).filter((x) => x.type === "noul" && x.target && x.target === q.target)
    .map((x) => [x, b.results[x.question_id]]).filter(([, r]) => r && r.probability_yes != null && r.probability_yes >= 0.5);
  const card = ([q, r]) => `<div class="gcard"><span>${esc(q.target || q.question_id)}</span>
      ${r && r.probabilities ? `<span class="num muted">${esc(L.pOf)} ${n2(r.probabilities[r.choice])}</span>` : ""}
      ${flagsFor(q).map(([x, fr]) => `<span class="tag">${esc(x.label.slice(0, 24))} · ${esc(L.pYesShort)} ${n2(fr.probability_yes)}</span>`).join("")}</div>`;
  return `<div class="groups">${options.map((o) => `<div class="gcol"><div class="gh">${esc(dim[0].options[o])} <span class="num">${cols[o].length}</span></div>${cols[o].map(card).join("")}</div>`).join("")}
    ${unassigned.length ? `<div class="gcol un"><div class="gh">${esc(L.unassigned)} <span class="num">${unassigned.length}</span></div>${unassigned.map(card).join("")}</div>` : ""}</div>`;
}

// M03: material regrouped by what the policy did with it. Only the first
// twelve rows move; the rest are counted and listed, never flown about.
export function evidenceHTML(run, key, L) {
  const e = run.evidence[key];
  if (!e) return "";
  const rounds = e.rounds.map((round) => {
    const g = groupItems(round);
    const anim = claim(run.run_id, `ev:${key}:${round.round}:${round.evidence_set_id}`, "regroup");
    let moved = 0;
    const row = (it) => {
      const cls = anim && moved++ < MAX_ANIMATED ? anim : "";
      return `<li class="it ${cls}" data-id="${esc(it.id)}"><span class="src">${esc(it.source || "")}</span>
        <span class="tt">${esc(it.title || "")}</span>
        <span class="num">${it.probability_yes == null ? esc(L.unavailable) : `${esc(L.pYesShort)} ${n2(it.probability_yes)}`}</span>
        ${it.visible ? `<span class="muted num">${esc(L.visibleRange(it.visible))}</span>` : ""}</li>`;
    };
    return `<div class="round"><div class="rh"><span class="lbl">${esc(L.round(round.round))}</span>
        <span class="meta num">${esc(L.groupsCount(round.counts))}</span></div>
      <div class="rc num">${esc(L.discoveredLine(round.counts))}</div>
      ${GROUPS.map((k) => g[k].length ? `<details class="grp g-${k}" ${k === "kept" || k === "review" ? "open" : ""}>
        <summary><span>${esc(L.groups[k])}</span><span class="num">${g[k].length}</span></summary>
        <ul>${g[k].map(row).join("")}</ul></details>` : "").join("")}
      ${round.total != null && round.total > (round.items || []).length ? `<div class="muted">${esc(L.moreItems(round.total - round.items.length))}</div>` : ""}</div>`;
  }).join("");
  const checks = e.checks.map((c) => `<div class="echeck ${esc(c.result)}"><b>${esc(L.sufficiency[c.result] || c.result)}</b>
      ${c.probability_yes != null ? `<span class="num">${esc(L.pYes)} ${n2(c.probability_yes)} · ${esc(L.threshold)} ${n2(c.threshold)}</span>` : ""}
      <span class="muted">${esc(c.scope === "delivery" ? L.deliveryCheck : L.checkedDelivered(c.delivered, c.delivered_chars))}</span>
      ${c.reason ? `<span class="muted">${esc(c.reason)}</span>` : ""}
      ${techHTML([[L.tech.evidenceSet, c.checked_set_id || c.evidence_set_id], [L.tech.limit, c.limit]], L)}</div>`).join("");
  const stop = e.stop ? `<div class="estop"><b>${esc(L.stops[e.stop.stop_reason] || e.stop.stop_reason)}</b>
      ${(e.stop.gaps || []).map((x) => `<span class="gapnote">${esc(x)}</span>`).join("")}</div>` : "";
  return `<section class="evid" data-key="evidence:${esc(key)}"><div class="bh"><span class="lbl">${esc(key ? L.stepMaterial : L.material)}</span></div>
    <p class="note">${esc(L.excludedNote)}</p>${rounds}${checks}${stop}</section>`;
}

// M04: Jev's reading of a step, then the policy's pick, joined by one short line.
export function routingHTML(run, L, titles = {}) {
  if (!run.routingOrder.length) return "";
  const rows = run.routingOrder.map((key) => {
    const r = run.routing[key];
    const a = r.assessed, s = r.selected, u = r.unavailable;
    const title = (a && a.title) || (s && s.title) || (u && u.title) || titles[key] || key;
    const phase = u ? "unavailable" : s ? "selected" : "assessed";
    const anim = claim(run.run_id, `route:${key}`, phase);
    const cands = ((s && s.candidates) || (u && u.candidates) || []).map((c) =>
      `<li class="${c.selected ? "on" : ""} ${c.eligible ? "" : "below"}"><span>${esc(c.label)}</span>
        <span class="num">L${esc(c.capability)}</span>
        <span class="muted">${esc(c.eligible ? (c.price === "unknown" ? L.priceUnknown : c.price === "free" ? L.freeAtRate : L.byCost) : L.belowLevel)}</span></li>`).join("");
    const need = (s && s.required_capability) ?? (u && u.required_capability);
    const skipped = run.skipped.some((x) => x.subtask_id === key);
    return `<div class="rt ${esc(phase)} ${anim}" data-key="route:${esc(key)}">
      <div class="rtt">${esc(title)}${skipped ? ` <span class="tag">${esc(L.generationSkipped)}</span>` : ""}</div>
      <div class="rtl">
        <div class="rside jev"><span class="who">${esc(L.assessedByJev)}</span>
          ${a && a.raw_score != null ? `<span class="num">${esc(L.rawScore)} ${n2(a.raw_score)} · ${esc(L.onScale(0, (a.levels || 5) - 1))}</span>
            <span class="num muted">${esc(L.confidence)} ${a.confidence == null ? esc(L.unavailable) : n2(a.confidence)}</span>` : `<span class="unav">${esc(L.unavailable)}</span>`}
          ${a && a.role ? `<span class="muted">${esc(L.role)}: ${esc(a.role)}${a.role_source === "plan" ? ` (${esc(L.fromPlan)})` : ""}</span>` : ""}</div>
        <span class="link" aria-hidden="true"></span>
        <div class="rside pol"><span class="who">${esc(u ? L.noQualified : L.selectedByPolicy)}</span>
          <span class="num">${esc(L.requiredLevel)} ${esc(need ?? "—")}</span>
          ${s ? `<b>${esc((s.selected && s.selected.label) || "")}</b>${s.downgraded ? ` <span class="tag warn">${esc(L.downgraded)}</span>` : ""}` : ""}
          ${u ? `<span class="muted">${esc(L.bestAvailable(u.best_available))}</span>` : ""}</div></div>
      ${cands ? `<details class="cands"><summary>${esc(L.candidates)}</summary><ul>${cands}</ul>
        <p class="note">${esc(L.costBasis)}</p></details>` : ""}
      ${s && (s.rules || []).length ? `<details class="why"><summary>${esc(L.whyThis)}</summary><ul>
        <li>${esc(L.routeWhy(need, ((s.candidates || []).filter((c) => c.eligible)).length))}</li>
        ${s.rules.map((x) => `<li><code>${esc(x)}</code> ${esc(ruleText({ rule: x, inputs: { raw: s.raw_score, adopted: s.adopted } }, L))}</li>`).join("")}</ul></details>` : ""}
    </div>`;
  }).join("");
  return `<section class="routing" data-key="routing"><div class="bh"><span class="lbl">${esc(L.routingTitle)}</span></div>${rows}</section>`;
}

// M05: only a confirmed skip is shown as one, and "0 LLM calls" only when true.
export function generationHTML(run, L) {
  if (!run.skipped.length) return "";
  const gen = generationClaim(run);
  const rows = run.skipped.map((s) => {
    const anim = claim(run.run_id, `skip:${s.subtask_id || "run"}`, "skip");
    return `<div class="skip ${anim}"><b>${esc(L.generationSkipped)}</b>
      <span class="muted">${esc(s.scope === "run" ? L.scopeRun : L.scopeStep(s.title || ""))}</span>
      <span>${esc(s.reason || "")}</span><code>${esc(s.rule || "")}</code></div>`;
  }).join("");
  const claimLine = gen ? `<div class="claim">${esc(gen.kind === "zero" ? L.zeroLLM : L.noGenCallStep)}</div>` : "";
  return `<section class="gen" data-key="generation"><div class="bh"><span class="lbl">${esc(L.generationTitle)}</span></div>${rows}${claimLine}</section>`;
}

// M06: each kind of doubt keeps its own name, and a later attempt never erases
// the earlier record.
export function reviewHTML(run, L) {
  if (!run.reviews.length) return "";
  const rows = run.reviews.map((r, i) => {
    const anim = claim(run.run_id, `review:${i}`, "enter");
    const later = run.policies.filter((p) => p.attempt_id && p.attempt_id !== "attempt-1" && (p.batch_id === r.batch_id || (r.subtask_id && p.subtask_id === r.subtask_id)));
    return `<div class="rv ${esc(r.reason)} ${anim}"><span class="tag ${r.reason === "low_confidence" ? "warn" : "bad"}">${esc(reasonLabel(r.reason, L))}</span>
      <span>${esc(r.detail || "")}</span>${r.question_id ? `<code>${esc(r.question_id)}</code>` : ""}
      ${r.threshold && r.threshold.rule ? `<span class="muted num">${esc(r.threshold.measure || "")} ${r.threshold.low != null ? `${n2(r.threshold.low)}–${n2(r.threshold.high)}` : `< ${n2(r.threshold.below)}`}</span>` : ""}
      ${later.map((p) => `<span class="after">${esc(L.laterAttempt(p.attempt_id))}: ${esc(ruleText(p, L))}</span>`).join("")}</div>`;
  }).join("");
  return `<section class="reviews" data-key="review"><div class="bh"><span class="lbl">${esc(L.needsReview)}</span></div>${rows}</section>`;
}

// The loop, as it ran: each request, and what it asked the tools for.
export function stepsHTML(run, L) {
  if (!run.steps.length) return "";
  const rows = run.steps.map((step) => {
    const anim = claim(run.run_id, `step:${step.subtask_id || ""}:${step.step}`, step.status === "running" ? "enter" : "settle");
    const tools = step.tools.map((call) => {
      const state = !call.allowed ? "refused" : call.status;
      return `<li class="tcall ${esc(state)}"><span class="tn">${esc(call.name)}</span>
        <span class="ta num">${esc(argsLine(call.arguments))}</span>
        <span class="ts">${esc(L.toolStates[state] || state)}${call.ms ? ` · ${call.ms} ms` : ""}</span>
        ${call.detail ? `<span class="td">${esc(call.detail)}</span>` : ""}
        ${!call.allowed ? `<span class="td">${esc(L.refusals[call.reason] || call.reason || "")}</span>` : ""}</li>`;
    }).join("");
    return `<div class="stp ${esc(step.status)} ${anim}">
      <div class="sh"><span class="lbl">${esc(L.stepN(step.step))}</span>
        <span class="meta num">${esc(step.status === "running" ? L.waitingModel
          : step.status === "answered" ? L.answered
          : step.status === "failed" ? L.failed
          : L.calledTools(step.tools.length))}</span></div>
      ${tools ? `<ul class="tcalls">${tools}</ul>` : ""}</div>`;
  }).join("");
  const compacted = run.compactions.map((c) => `<div class="compact">${esc(L.compactedLine(c.before_tokens, c.after_tokens, c.shadowed))}</div>`).join("");
  return `<section class="steps" data-key="steps"><div class="bh"><span class="lbl">${esc(L.stepsTitle)}</span>
      <span class="meta num">${esc(L.stepsLine(run.steps.length, run.toolsRun))}</span></div>
    ${rows}${compacted}</section>`;
}
const argsLine = (args) => {
  const text = Object.entries(args || {}).map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`).join(" ");
  return text.length > 90 ? text.slice(0, 89) + "…" : text;
};

export function costHTML(run, L) {
  const tl = tally(run);
  const src = Object.entries(tl.sources).map(([k, v]) => `${L.costSources[k] || k} ${v}`).join(" · ");
  return `<section class="costs" data-key="costs"><div class="bh"><span class="lbl">${esc(L.costsTitle)}</span></div>
    <div class="meters">
      <div><div class="lbl">${esc(L.judgements)}</div><div class="v num">${tl.judgements}</div></div>
      <div><div class="lbl">${esc(L.jevCalls)}</div><div class="v num">${tl.jevCalls}</div></div>
      <div><div class="lbl">${esc(L.llmCalls)}</div><div class="v num">${tl.llmCalls}</div></div>
      <div><div class="lbl">${esc(L.knownCost)}</div><div class="v num">${esc(money(tl.knownUsd))}</div></div>
      ${tl.unknownCalls ? `<div><div class="lbl">${esc(L.unknownCost)}</div><div class="v num">${tl.unknownCalls}</div></div>` : ""}</div>
    <p class="note">${esc(src)}${tl.freeAtRate ? ` · ${esc(L.freeAtRate)} × ${tl.freeAtRate}` : ""}</p></section>`;
}

export function outputsHTML(run, L) {
  const targets = Object.keys(run.outputs);
  if (!targets.length) return "";
  const stale = (run.outputs.answer || []).slice(-1)[0];
  const needsSync = stale && stale.needs_sync;
  return `<section class="outs" data-key="outputs"><div class="bh"><span class="lbl">${esc(L.outputsTitle)}</span></div>
    ${targets.map((k) => { const v = run.outputs[k].slice(-1)[0]; return `<div class="ov"><span>${esc(k === "answer" ? L.wholeAnswer : v.title || k)}</span>
      <span class="num">${v.version != null ? `v${esc(v.version)}` : "—"}</span><span class="muted">${esc(L.modes[v.mode] || v.mode || "")}</span>
      ${(v.depends_on || []).length ? `<span class="muted num">${esc(L.dependsOn)} ${esc(v.depends_on.join(", "))}</span>` : ""}</div>`; }).join("")}
    ${needsSync ? `<div class="rblock"><b>${esc(L.needsSync)}</b><span>${esc(L.needsSyncNote)}</span><span class="acts"><button class="btn sm" data-dp-action="sync">${esc(L.syncNow)}</button></span></div>` : ""}</section>`;
}

export function componentsHTML(run, L) {
  const c = run.components;
  if (!c) return "";
  const slots = (c.slots && c.slots.components) || [];
  const state = c.state === "failed" ? L.componentFailed : c.state === "ready" ? L.componentReady : L.componentSlots;
  return `<section class="comps" data-key="components"><div class="bh"><span class="lbl">${esc(L.componentsTitle)}</span>
      <span class="st">${esc(state)}</span></div>
    ${c.slots && c.slots.kind === "code" ? `<p class="note">${esc(L.codeKind)}</p>` : `<div class="slots">${slots.map((s) => `<span class="slot ${c.state === "ready" ? "full" : ""}">${esc(s)}</span>`).join("")}</div>`}
    ${c.filled && c.filled.example_data ? `<p class="tag warn">${esc(L.exampleData)}</p>` : ""}
    ${c.failed ? `<p class="bad">${esc(c.failed.reason || "")}</p>` : ""}</section>`;
}

// The whole view, as keyed sections.
export function decisionsParts(run, L, opts = {}) {
  const parts = [];
  const badge = opts.source === "demo" ? `<span class="badge demo">${esc(L.sources_.demo)}</span>`
    : opts.source === "recorded" ? `<span class="badge">${esc(L.sources_.recorded)}</span>` : `<span class="badge live">${esc(L.sources_.live)}</span>`;
  parts.push(["head", `<div class="dhead">${badge}<span class="muted num">${esc(run.run_id)}</span>
    ${opts.source === "demo" ? `<p class="note">${esc(L.demoNote)}</p>` : ""}

    ${run.gaps.length ? `<p class="rgap">${esc(L.recordGap(run.gaps))}</p>` : ""}
    ${opts.truncated ? `<button class="btn ghost sm" data-dp-action="load_full">${esc(L.loadFull)}</button>` : ""}</div>`]);
  parts.push(["status", statusBarHTML(run, L, { ...opts, noButton: true })]);
  const gen = generationHTML(run, L); if (gen) parts.push(["generation", gen]);
  const rev = reviewHTML(run, L); if (rev) parts.push(["review", rev]);
  const rt = routingHTML(run, L, opts.titles || {}); if (rt) parts.push(["routing", rt]);
  const steps = stepsHTML(run, L); if (steps) parts.push(["steps", steps]);
  for (const key of run.evidenceOrder) parts.push([`evidence:${key}`, evidenceHTML(run, key, L)]);
  // The newest batches first-class; older ones one click away, so a long run
  // never puts thousands of rows in the page at once.
  const limit = opts.batchLimit || BATCHES_SHOWN;
  const order = run.batchOrder.slice(-limit);
  if (run.batchOrder.length > order.length) {
    parts.push(["batches-more", `<div class="dmore"><button class="btn ghost sm" data-dp-more="batches">${esc(L.showEarlier(run.batchOrder.length - order.length))}</button></div>`]);
  }
  for (const id of order) parts.push([`batch:${id}`, batchCardHTML(run, run.batches[id], L, opts)]);
  const comp = componentsHTML(run, L); if (comp) parts.push(["components", comp]);
  const outs = outputsHTML(run, L); if (outs) parts.push(["outputs", outs]);
  parts.push(["costs", costHTML(run, L)]);
  if (opts.source !== "demo") parts.push(["export", `<div class="dfoot"><button class="btn ghost sm" data-dp-action="export">${esc(L.exportRecord)}</button></div>`]);
  return parts;
}

/* ---------- DOM: keyed patching ---------- */
// Replace only the parts whose HTML changed; keep the rest — and whatever the
// reader opened, selected or focused inside them — exactly as it is.
export function patch(host, parts) {
  const keep = new Set(parts.map(([k]) => k));
  for (const child of [...host.children]) if (!keep.has(child.dataset.part)) child.remove();
  let prev = null;
  for (const [key, html] of parts) {
    let el = host.querySelector(`:scope > [data-part="${CSS.escape(key)}"]`);
    if (!el) {
      el = document.createElement("div");
      el.dataset.part = key;
      el._html = null;
    }
    if (el._html !== html) {
      const open = [...el.querySelectorAll("details")].map((d) => d.open);
      el.innerHTML = html;
      el._html = html;
      // Details the reader opened stay open across updates of the same part.
      const now = [...el.querySelectorAll("details")];
      if (open.length === now.length) now.forEach((d, i) => { if (open[i]) d.open = true; });
    }
    const want = prev ? prev.nextSibling : host.firstChild;
    if (want !== el) host.insertBefore(el, want);
    prev = el;
  }
}

// Live elapsed time: text only, no re-render.
let ticker = 0;
export function startTicker() {
  if (ticker || typeof document === "undefined") return;
  ticker = setInterval(() => {
    if (document.hidden) return;
    const now = Date.now();
    document.querySelectorAll("[data-rtime]").forEach((el) => {
      el.textContent = secs(now - Number(el.dataset.rtime));
    });
  }, 250);
}
