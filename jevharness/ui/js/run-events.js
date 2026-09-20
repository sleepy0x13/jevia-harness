// The decision-event reducer. Pure: no DOM, no timers, no network.
//
// Every view of a run — live, recorded, or a labelled demo — is built by
// feeding events through `reduce`. The reducer commits the true state at once;
// animation lives elsewhere (motion.js) and can never hold a state change back.
//
// Guarantees the tests rely on:
// * an event is applied once (event_id), and only to its own run (run_id);
// * a late or repeated event never moves a finished batch or run backwards;
// * gaps in `seq` are recorded, and filled when the missing event turns up;
// * a charged request is counted once (call_id), whatever mentions it.

export const EVENT_VERSION = 1;
const TERMINAL_BATCH = new Set(["completed", "failed"]);
const TERMINAL_RUN = new Set(["completed", "needs_review", "cancelled", "failed", "partial"]);

export function createRun(runId, source = "live") {
  return {
    run_id: runId, source, version: 0,
    lastSeq: 0, seen: new Set(), gaps: [], count: 0, unknownKinds: 0,
    status: "running", stage: null, actor: null, stageAt: 0, elapsed_ms: 0,
    batches: {}, batchOrder: [],
    policies: [], reviews: [], skipped: [],
    evidence: {}, evidenceOrder: [],
    routing: {}, routingOrder: [],
    llm: { started: 0, completed: 0, failed: 0 },
    // The agent loop: one entry per step, each with the tools it asked for.
    steps: [], toolsRun: 0, compactions: [],
    // Questions the run is waiting on — parallel workers can each have one —
    // and the plan it offered for approval.
    questions: [], plan: null,
    calls: {}, outputs: {}, components: null, loops: [],
    completed: null, cancel: { requested: false, confirmed: false, may_bill: null },
    failure: null, operations: new Set(),
  };
}

// Stage → fixed interface label key (copy.js `stages`). Never the task's language.
export const STAGE_KEYS = {
  run: "running", assess: "assessing", plan: "planning", search: "searching", fetch: "reading",
  source_triage: "choosingSources", evidence_filter: "classifying", evidence_check: "checkingEvidence",
  routing: "routing", decisions: "judging", generation: "writing", assembly: "assembling",
  guard: "checkingAnswer", review: "reviewing", tools: "runningTools", app: "buildingApp",
  revision: "revising", research: "searching",
};

function markSeq(run, seq) {
  if (typeof seq !== "number" || !Number.isFinite(seq)) return;
  if (seq > run.lastSeq + 1) run.gaps.push([run.lastSeq + 1, seq - 1]);
  else if (seq <= run.lastSeq) {
    // A late event: it may fill a recorded gap.
    run.gaps = run.gaps.flatMap(([a, b]) => {
      if (seq < a || seq > b) return [[a, b]];
      const out = [];
      if (a <= seq - 1) out.push([a, seq - 1]);
      if (seq + 1 <= b) out.push([seq + 1, b]);
      return out;
    });
  }
  run.lastSeq = Math.max(run.lastSeq, seq);
}

function noteCall(run, usage, actor) {
  if (!usage || !usage.call_id) return;
  const prev = run.calls[usage.call_id];
  // A later, better-known figure lands on the same call; it never adds a second.
  if (!prev || (prev.cost_usd == null && usage.cost_usd != null)) {
    run.calls[usage.call_id] = { cost_usd: usage.cost_usd, cost_source: usage.cost_source || "unknown", actor: actor || (prev && prev.actor) };
  }
}

// The step a tool call belongs to: the newest open one for that worker.
function lastStep(run, subtask, number) {
  for (let i = run.steps.length - 1; i >= 0; i--) {
    const step = run.steps[i];
    if ((step.subtask_id || null) !== (subtask || null)) continue;
    if (number == null || step.step === number) return step;
  }
  return null;
}
function findCall(run, id) {
  for (let i = run.steps.length - 1; i >= 0; i--) {
    const call = run.steps[i].tools.find((call) => call.call_id === id);
    if (call) return call;
  }
  return null;
}

function evidenceFor(run, key) {
  if (!run.evidence[key]) { run.evidence[key] = { rounds: [], checks: [], stop: null }; run.evidenceOrder.push(key); }
  return run.evidence[key];
}
function routeFor(run, key) {
  if (!run.routing[key]) { run.routing[key] = { subtask_id: key, assessed: null, selected: null, unavailable: null }; run.routingOrder.push(key); }
  return run.routing[key];
}

// Apply one event. Returns the phases that changed, for the motion controller:
// [{ key, phase }]. An empty list means nothing to show.
export function reduce(run, ev) {
  if (!ev || ev.type !== "decision_event") return [];
  if (ev.run_id !== run.run_id) return [];                 // never another run's result
  if (ev.event_id && run.seen.has(ev.event_id)) return [];  // duplicates change nothing
  if (ev.event_version !== EVENT_VERSION) { run.unknownKinds++; }
  if (ev.event_id) run.seen.add(ev.event_id);
  markSeq(run, ev.seq);
  run.count++; run.version++;
  if (ev.operation_id) run.operations.add(ev.operation_id);
  const late = typeof ev.seq === "number" && ev.seq < run.lastSeq;
  const p = ev.payload || {};
  const changes = [];
  if (typeof ev.elapsed_ms === "number") run.elapsed_ms = Math.max(run.elapsed_ms, ev.elapsed_ms);
  if (!late && !TERMINAL_RUN.has(run.status) && ev.stage && ev.kind !== "run.completed") {
    if (ev.stage !== run.stage || ev.actor !== run.actor) { run.stage = ev.stage; run.actor = ev.actor; run.stageAt = ev.elapsed_ms || 0; changes.push({ key: "stage", phase: ev.stage }); }
  }
  noteCall(run, ev.usage, ev.actor);

  switch (ev.kind) {
    case "batch.started": {
      const b = run.batches[ev.batch_id];
      if (b && TERMINAL_BATCH.has(b.status)) break;        // a late start never reopens a batch
      run.batches[ev.batch_id] = {
        batch_id: ev.batch_id, stage: ev.stage, purpose: p.purpose, subtask_id: ev.subtask_id,
        attempt_id: ev.attempt_id, operation_id: ev.operation_id, status: "pending",
        questions: p.questions || [], results: {}, missing: [], started_ms: ev.elapsed_ms, seq: ev.seq,
      };
      if (!b) run.batchOrder.push(ev.batch_id);
      changes.push({ key: `batch:${ev.batch_id}`, phase: "enter" });
      break;
    }
    case "batch.completed":
    case "batch.failed": {
      let b = run.batches[ev.batch_id];
      if (b && TERMINAL_BATCH.has(b.status)) break;         // one final state per batch
      if (!b) {                                              // the start was lost: still show the result
        b = run.batches[ev.batch_id] = { batch_id: ev.batch_id, stage: ev.stage, purpose: p.purpose, subtask_id: ev.subtask_id,
          attempt_id: ev.attempt_id, operation_id: ev.operation_id, questions: [], results: {}, missing: [], started_ms: null, seq: ev.seq };
        run.batchOrder.push(ev.batch_id);
      }
      b.ended_ms = ev.elapsed_ms;
      b.usage = ev.usage || null;
      if (ev.kind === "batch.completed") {
        b.status = "completed";
        for (const r of p.results || []) b.results[r.question_id] = r;
        b.missing = p.missing || [];
      } else {
        b.status = "failed";
        b.reason = p.reason || "";
      }
      changes.push({ key: `batch:${ev.batch_id}`, phase: "settle" });
      break;
    }
    case "policy.applied":
      run.policies.push({ ...p, batch_id: ev.batch_id, question_id: ev.question_id, subtask_id: ev.subtask_id,
        attempt_id: ev.attempt_id, actor: ev.actor, stage: ev.stage, seq: ev.seq });
      break;
    case "policy.review_required":
      run.reviews.push({ ...p, batch_id: ev.batch_id, question_id: ev.question_id, subtask_id: ev.subtask_id,
        attempt_id: ev.attempt_id, stage: ev.stage, seq: ev.seq });
      changes.push({ key: `review:${run.reviews.length - 1}`, phase: "enter" });
      break;
    case "evidence.filtered": {
      const e = evidenceFor(run, ev.subtask_id || "");
      if (!e.rounds.some((r) => r.round === p.round && r.evidence_set_id === ev.evidence_set_id)) {
        e.rounds.push({ round: p.round, counts: p.counts || {}, items: p.items || [], total: p.items_total,
          review_policy: p.review_policy, evidence_set_id: ev.evidence_set_id, seq: ev.seq });
        changes.push({ key: `evidence:${ev.subtask_id || ""}:${p.round}`, phase: "regroup" });
      }
      break;
    }
    case "evidence.checked":
      evidenceFor(run, ev.subtask_id || "").checks.push({ ...p, evidence_set_id: ev.evidence_set_id, stage: ev.stage, seq: ev.seq, actor: ev.actor });
      changes.push({ key: `check:${ev.subtask_id || ""}`, phase: "settle" });
      break;
    case "evidence.stopped":
      evidenceFor(run, ev.subtask_id || "").stop = { ...p, evidence_set_id: ev.evidence_set_id };
      break;
    case "routing.assessed":
      routeFor(run, ev.subtask_id || "_answer").assessed = { ...p, batch_id: ev.batch_id };
      changes.push({ key: `route:${ev.subtask_id || "_answer"}`, phase: "assessed" });
      break;
    case "routing.selected":
      routeFor(run, ev.subtask_id || "_answer").selected = { ...p, batch_id: ev.batch_id, stage: ev.stage };
      changes.push({ key: `route:${ev.subtask_id || "_answer"}`, phase: "selected" });
      break;
    case "routing.unavailable":
      routeFor(run, ev.subtask_id || "_answer").unavailable = { ...p, batch_id: ev.batch_id, stage: ev.stage };
      changes.push({ key: `route:${ev.subtask_id || "_answer"}`, phase: "unavailable" });
      break;
    case "generation.skipped":
      run.skipped.push({ ...p, subtask_id: ev.subtask_id });
      changes.push({ key: `skip:${ev.subtask_id || ""}`, phase: "skip" });
      break;
    case "step.started":
      run.steps.push({ step: p.step, subtask_id: ev.subtask_id, tools: [], status: "running",
        offered: p.tools_offered, context_tokens: p.context_tokens, seq: ev.seq });
      changes.push({ key: `step:${ev.subtask_id || ""}:${p.step}`, phase: "enter" });
      break;
    case "step.completed": {
      const step = lastStep(run, ev.subtask_id, p.step);
      if (step) { step.status = p.outcome || "done"; step.chars = p.chars; step.detail = p.detail; }
      changes.push({ key: `step:${ev.subtask_id || ""}:${p.step}`, phase: "settle" });
      break;
    }
    case "tool.called": {
      const step = lastStep(run, ev.subtask_id);
      const call = { call_id: p.call_id, name: p.tool, arguments: p.arguments,
        allowed: p.allowed, reason: p.reason, status: p.allowed ? "running" : "refused" };
      (step ? step.tools : (run.steps[run.steps.length - 1] || { tools: [] }).tools).push(call);
      changes.push({ key: `tool:${p.call_id}`, phase: "enter" });
      break;
    }
    case "tool.result": {
      const call = findCall(run, p.call_id);
      if (call) { call.status = p.ok ? "ok" : "failed"; call.detail = p.detail; call.ms = p.ms; call.chars = p.chars; }
      if (p.ok) run.toolsRun++;
      changes.push({ key: `tool:${p.call_id}`, phase: "settle" });
      break;
    }
    case "question.asked":
      if (!run.questions.some((q) => q.id === ev.question_id)) {
        run.questions.push({ id: ev.question_id, text: p.question, options: p.options || [],
          kind: p.kind || "question", subtask_id: ev.subtask_id, asked_ms: ev.elapsed_ms });
      }
      changes.push({ key: `question:${ev.question_id}`, phase: "enter" });
      break;
    case "question.answered": {
      const asked = run.questions.find((q) => q.id === ev.question_id);
      if (asked) { asked.answer = p.answer; asked.source = p.source; }
      changes.push({ key: `question:${ev.question_id}`, phase: "settle" });
      break;
    }
    case "plan.proposed":
      run.plan = { text: p.plan, steps: p.step_count, decided: null };
      break;
    case "plan.decided":
      if (run.plan) { run.plan.decided = !!p.approved; run.plan.feedback = p.feedback; }
      break;
    case "context.compacted":
      run.compactions.push({ ...p, seq: ev.seq });
      changes.push({ key: `compact:${run.compactions.length}`, phase: "enter" });
      break;
    case "llm.started": run.llm.started++; break;
    case "llm.completed": run.llm.completed++; break;
    case "llm.failed": run.llm.failed++; break;
    case "output.version": {
      const list = run.outputs[p.target] || (run.outputs[p.target] = []);
      list.push({ ...p, operation_id: ev.operation_id, seq: ev.seq });
      break;
    }
    case "component.slots": run.components = { ...(run.components || {}), slots: p, state: "slots" }; break;
    case "component.filled": run.components = { ...(run.components || {}), filled: p, state: "ready" }; break;
    case "component.failed": run.components = { ...(run.components || {}), failed: p, state: "failed" }; break;
    case "loop.iteration": run.loops.push(p); break;
    case "run.cancel_requested": run.cancel.requested = true; if (!TERMINAL_RUN.has(run.status)) run.status = "cancel_requested"; break;
    case "run.cancelled":
      run.cancel.confirmed = true; run.cancel.may_bill = !!p.may_bill; run.status = "cancelled";
      changes.push({ key: "status", phase: "cancelled" });
      break;
    case "run.failed": if (!TERMINAL_RUN.has(run.status)) { run.status = "failed"; run.failure = p; } break;
    case "run.completed":
      run.completed = p;
      if (run.status !== "cancelled") run.status = p.status || "completed";
      run.stage = null; run.actor = null;
      changes.push({ key: "status", phase: "done" });
      break;
    case "run.started": case "stage.entered": break;
    default: run.unknownKinds++; break;           // newer servers may send more; ignore safely
  }
  return changes;
}

export function replayAll(runId, events, source) {
  const run = createRun(runId, source);
  for (const ev of events || []) reduce(run, ev);
  return run;
}

/* ---------- derived views (still pure) ---------- */

// Judgements and API calls are different numbers and are never merged.
// The questions still waiting on a person. Parallel workers may each have one.
export function openQuestions(run) {
  return run.questions.filter((q) => q.answer === undefined);
}

export function tally(run) {
  const calls = Object.entries(run.calls);
  const jevCalls = run.batchOrder.length;
  const questions = run.batchOrder.reduce((n, id) => n + ((run.batches[id].questions || []).length || Object.keys(run.batches[id].results).length), 0);
  const known = calls.filter(([, c]) => c.cost_usd != null && c.cost_source !== "unknown");
  const unknown = calls.length - known.length;
  const llm = run.completed ? run.completed.llm_calls : run.llm.started;
  return {
    judgements: questions, jevCalls, llmCalls: llm,
    knownUsd: known.reduce((s, [, c]) => s + (c.cost_usd || 0), 0), unknownCalls: unknown,
    freeAtRate: known.filter(([, c]) => c.cost_usd === 0 && c.actor === "llm").length,
    sources: calls.reduce((m, [, c]) => { m[c.cost_source] = (m[c.cost_source] || 0) + 1; return m; }, {}),
  };
}

// "0 LLM calls" is a claim about the whole run, and only true when it is.
export function generationClaim(run) {
  const skipRun = run.skipped.find((s) => s.scope === "run");
  if (!skipRun) return null;
  const llm = run.completed ? run.completed.llm_calls : null;
  if (llm === 0) return { kind: "zero" };
  return { kind: "step", llm };
}

// Which question in a batch a policy row read, so a result can show what was done with it.
export function policiesFor(run, batchId, questionId) {
  return run.policies.filter((p) => p.batch_id === batchId && (!questionId || !p.question_id || p.question_id === questionId));
}

// Items grouped for the material view. Order inside a group is the order they arrived.
export const GROUPS = ["kept", "review", "excluded", "not_assessed"];
export function groupItems(round) {
  const out = { kept: [], review: [], excluded: [], not_assessed: [] };
  for (const it of round.items || []) (out[it.status] || out.not_assessed).push(it);
  return out;
}

// For M02's grouping: choice questions in one batch sharing one option set.
export function choiceDimensions(batch) {
  const byKey = {};
  for (const q of batch.questions || []) {
    if (q.type !== "choice" || !q.options) continue;
    const key = Object.keys(q.options).sort().join("|");
    (byKey[key] = byKey[key] || []).push(q);
  }
  return Object.values(byKey).filter((qs) => qs.length >= 3);
}

export function summaryItems(run) {
  const items = [];
  const assess = run.policies.filter((p) => p.stage === "assess");
  if (assess.length) items.push({ key: "assess", kind: "assess", rows: assess });
  for (const key of run.evidenceOrder) {
    const e = run.evidence[key];
    if (key === "" && (e.rounds.length || e.checks.length)) items.push({ key: "evidence", kind: "evidence", e });
  }
  if (run.routingOrder.length) items.push({ key: "routing", kind: "routing", rows: run.routingOrder.map((k) => run.routing[k]) });
  const dec = run.batchOrder.map((id) => run.batches[id]).filter((b) => b.stage === "decisions");
  if (dec.length) items.push({ key: "decisions", kind: "decisions", batch: dec[dec.length - 1] });
  if (run.plan) items.push({ key: "plan", kind: "plan", plan: run.plan });
  if (run.steps.length > 1 || run.toolsRun) items.push({ key: "steps", kind: "steps",
    steps: run.steps, tools: run.toolsRun, compactions: run.compactions });
  const gen = generationClaim(run);
  if (gen) items.push({ key: "generation", kind: "skipped", gen, skip: run.skipped.find((s) => s.scope === "run") });
  if (run.reviews.length) items.push({ key: "review", kind: "review", rows: run.reviews });
  return items.slice(0, 6);
}
