// vNext regression cases, interface half: the event reducer, the motion
// controller and the pure renderers. No browser, no network.
//   node scripts/ui_tests.mjs
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import assert from "node:assert/strict";

const ui = join(dirname(fileURLToPath(import.meta.url)), "..", "jevharness", "ui");
const { COPY } = await import(join(ui, "js", "copy.js"));
const RE = await import(join(ui, "js", "run-events.js"));
const M = await import(join(ui, "js", "motion.js"));
const DP = await import(join(ui, "js", "decision-panel.js"));
const EN = COPY.en.dp, ZH = COPY.zh.dp;

const fixtures = JSON.parse(readFileSync(join(ui, "fixtures", "index.json"), "utf8")).fixtures
  .map((f) => ({ ...f, data: JSON.parse(readFileSync(join(ui, "fixtures", f.file), "utf8")) }));
const load = (name) => fixtures.find((f) => f.name === name).data.events;
const play = (events, id = "fixture-x") => RE.replayAll(id, events.map((e) => ({ ...e, run_id: id })), "demo");

const results = [];
function test(name, fn) {
  try { M._reset(); fn(); results.push(["ok", name]); } catch (e) { results.push(["FAIL", name, e]); }
}

const ev = (seq, kind, extra = {}) => ({
  type: "decision_event", event_version: 1, event_id: `r-e${seq}`, seq, run_id: "r", operation_id: "initial",
  attempt_id: "attempt-1", kind, actor: "jev", stage: "decisions", elapsed_ms: seq * 10, payload: {}, ...extra,
});

/* ---------- every fixture goes through the one reducer ---------- */
test("fixtures: five or more, all replay cleanly through the reducer", () => {
  assert.ok(fixtures.length >= 5);
  for (const f of fixtures) {
    const run = play(f.data.events, `fixture-${f.name}`);
    assert.equal(run.gaps.length, 0, f.name);
    assert.ok(["completed", "needs_review", "cancelled"].includes(run.status), `${f.name}: ${run.status}`);
    for (const id of run.batchOrder) assert.notEqual(run.batches[id].status, "pending", `${f.name} ${id}`);
    assert.ok(DP.decisionsParts(run, EN, { source: "demo" }).length > 2);
  }
});

/* ---------- T02: one call, many questions ---------- */
test("T02 eight questions are one call and settle together", () => {
  const run = RE.createRun("r");
  const qs = Array.from({ length: 8 }, (_, i) => ({ question_id: `q${i}`, type: "noul", label: `Q${i}` }));
  RE.reduce(run, ev(1, "batch.started", { batch_id: "b1", payload: { purpose: "decide", questions: qs, count: 8 } }));
  assert.equal(run.batches.b1.status, "pending");
  const strip = DP.batchStripHTML(run, run.batches.b1, EN);
  assert.ok(!/P\(yes\)\s*\d/.test(strip), "no value is pre-filled while waiting");
  RE.reduce(run, ev(2, "batch.completed", { batch_id: "b1", usage: { call_id: "c1", cost_usd: 0.00002, cost_source: "provider_reported" },
    payload: { results: qs.map((q) => ({ question_id: q.question_id, type: "noul", probability_yes: 0.9, confidence: null })), missing: [] } }));
  const t = RE.tally(run);
  assert.equal(t.jevCalls, 1); assert.equal(t.judgements, 8);
  assert.equal(Object.keys(run.calls).length, 1, "one charge");
  assert.equal(Object.keys(run.batches.b1.results).length, 8);
});

/* ---------- M01: failure is not "No" ---------- */
test("M01 a failed batch shows the failure, never No", () => {
  const run = RE.createRun("r");
  RE.reduce(run, ev(1, "batch.started", { batch_id: "b", payload: { questions: [{ question_id: "a", type: "noul" }], count: 1 } }));
  RE.reduce(run, ev(2, "batch.failed", { batch_id: "b", payload: { reason: "upstream HTTP 503" } }));
  const html = DP.batchCardHTML(run, run.batches.b, EN);
  assert.ok(html.includes("Jev request failed") || html.includes("Failed"));
  assert.ok(!/>\s*No\s*</.test(html));
});

/* ---------- T08 / T09: the three answer types ---------- */
test("T08 Noul P(yes)=0.02 says which way the probability points", () => {
  const html = DP.resultHTML({ type: "noul", probability_yes: 0.02, confidence: null }, { type: "noul" }, EN, { threshold: 0.55 });
  assert.ok(html.includes("P(yes)") && html.includes("0.02"));
  assert.ok(!html.includes("2%"), "never a bare percentage");
  assert.ok(!/confidence/i.test(html), "no confidence the API did not return");
});
test("T09 Choice: selected probability and Confidence are separate, labelled fields", () => {
  const html = DP.resultHTML({ type: "choice", choice: "a", probabilities: { a: 0.6, b: 0.25, c: 0.1, d: 0.03, e: 0.02 }, confidence: 0.3 },
    { type: "choice", options: { a: "Alpha", b: "B", c: "C", d: "D", e: "E" } }, EN);
  assert.ok(html.includes("Confidence") && html.includes("0.30"));
  assert.ok(html.includes("p 0.60"));
  assert.ok(html.includes("View all (5)"), "the tail is kept, one click away");
  assert.ok(html.includes("Total 1.00"), "no renormalising");
});
test("M02 Score shows its own scale, the raw value and the adopted level", () => {
  const html = DP.resultHTML({ type: "score", score: 2.4, probabilities: null, confidence: 0.5 },
    { type: "score", levels: ["a", "b", "c", "d", "e", "f", "g"] }, EN, { adopted: 3 });
  assert.ok(html.includes("2.40") && html.includes("on 0–6") && html.includes("Adopted"));
});
test("M02 missing fields are Unavailable, never 0", () => {
  const html = DP.resultHTML({ type: "noul", probability_yes: null }, null, EN);
  assert.ok(html.includes("Unavailable") && !html.includes("0.00"));
});

/* ---------- M03 / T05 ---------- */
test("M03 T05 material groups add up and Not assessed stays apart", () => {
  const run = play(load("research-filter"));
  const round = run.evidence[""].rounds[0];
  const c = round.counts;
  assert.equal(c.discovered, c.kept + c.review + c.excluded + c.not_assessed);
  const g = RE.groupItems(round);
  assert.equal(g.not_assessed.length, c.not_assessed);
  assert.ok(g.not_assessed.every((x) => x.probability_yes == null));
  const html = DP.evidenceHTML(run, "", EN);
  assert.ok(html.includes("Not assessed") && html.includes("Excluded means not sent to the writer"));
  assert.ok(run.evidence[""].checks.some((x) => x.result === "sufficient"));
  assert.equal(run.evidence[""].stop.stop_reason, "sufficient");
});
test("M03 at most twelve rows animate at once", () => {
  const run = play(load("research-filter"), "live-r");
  const html = DP.evidenceHTML(run, "", EN);
  assert.ok((html.match(/class="it m-play"/g) || []).length <= 12 * run.evidence[""].rounds.length);
});

/* ---------- M04 / T10 ---------- */
test("M04 routing shows Jev's reading and the policy's pick apart", () => {
  const run = play(load("multi-route"));
  const html = DP.routingHTML(run, EN);
  assert.ok(html.includes("Assessed by Jev") && html.includes("Selected by policy"));
  assert.ok(!/Jev (chose|selected|picked)/i.test(html), "a price-table pick is never credited to Jev");
  assert.ok(html.includes("Below required level"));
});
test("T10 no qualified model asks the user; nothing weaker is lit up", () => {
  const run = play(load("needs-review"));
  assert.equal(run.status, "needs_review");
  const card = DP.runCardParts(run, EN, {}).map(([, h]) => h).join("");
  assert.ok(card.includes("No qualified model") && card.includes("Allow downgrade") && card.includes("Add a model"));
  const route = run.routing._answer;
  assert.ok(route.unavailable && !route.selected);
});

/* ---------- M05 / T11 ---------- */
test("T11 a planning call means 'No generation call for this step', not 0 LLM calls", () => {
  const run = play(load("decide-no-generate"));
  const html = DP.generationHTML(run, EN);
  assert.ok(html.includes("No generation call for this step"));
  assert.ok(!html.includes("0 LLM calls"));
});
test("M05 0 LLM calls only when the whole run made none", () => {
  const run = play(load("feedback-zh"));
  assert.equal(run.completed.llm_calls, 0);
  assert.ok(DP.generationHTML(run, EN).includes("0 LLM calls"));
});

/* ---------- M06 ---------- */
test("M06 each kind of doubt keeps its own name", () => {
  const a = DP.reviewHTML(play(load("needs-review")), EN);
  const b = DP.reviewHTML(play(load("jev-failed")), EN);
  assert.ok(a.includes("Low confidence") && a.includes("No qualified model"));
  assert.ok(b.includes("Jev request failed") && !b.includes("Low confidence"));
});

/* ---------- T13: duplicates, lateness, gaps, parallel runs ---------- */
test("T13 duplicates, late and foreign events change nothing they should not", () => {
  const run = RE.createRun("r");
  const qs = [{ question_id: "a", type: "noul" }];
  RE.reduce(run, ev(1, "batch.started", { batch_id: "b", payload: { questions: qs } }));
  const done = ev(3, "batch.completed", { batch_id: "b", usage: { call_id: "c", cost_usd: null, cost_source: "unknown" },
    payload: { results: [{ question_id: "a", type: "noul", probability_yes: 0.7 }] } });
  RE.reduce(run, done);
  assert.deepEqual(run.gaps, [[2, 2]], "a missing event is visible");
  assert.deepEqual(RE.reduce(run, done), [], "a duplicate is ignored");
  RE.reduce(run, ev(2, "batch.started", { batch_id: "b", event_id: "late" }));
  assert.equal(run.batches.b.status, "completed", "a late start never reopens a batch");
  assert.deepEqual(run.gaps, [], "the late event fills the gap");
  RE.reduce(run, { ...ev(9, "batch.failed", { batch_id: "b" }), run_id: "other" });
  assert.equal(run.batches.b.status, "completed", "another run's event is never applied");
  RE.reduce(run, ev(4, "batch.failed", { batch_id: "b", event_id: "x4" }));
  assert.equal(run.batches.b.status, "completed", "one final state per batch");
  RE.reduce(run, ev(5, "batch.completed", { batch_id: "b", event_id: "x5", usage: { call_id: "c", cost_usd: 0.1, cost_source: "provider_reported" } }));
  assert.equal(Object.keys(run.calls).length, 1, "the same call is never charged twice");
  assert.equal(run.calls.c.cost_usd, 0.1, "a later known figure lands on the same call");
});
test("T13 a finished run is not moved back by a stale event", () => {
  const run = RE.createRun("r");
  RE.reduce(run, ev(2, "run.completed", { payload: { status: "completed", llm_calls: 0 } }));
  RE.reduce(run, ev(1, "stage.entered", { stage: "generation", actor: "llm" }));
  assert.equal(run.status, "completed");
  assert.equal(run.stage, null);
});

/* ---------- T14: streaming tokens do not replay anything ---------- */
test("T14 rebuilding the card during streaming does not replay its animation", () => {
  const events = load("multi-route").map((e) => ({ ...e, run_id: "live-1" }));
  const run = RE.createRun("live-1");
  for (const e of events) RE.reduce(run, e);
  const first = DP.runCardParts(run, EN, {}).map(([, h]) => h).join("");
  // 700 ms later the same state renders static: one play, then rest.
  const origNow = performance.now.bind(performance);
  performance.now = () => origNow() + 5000;
  try {
    const later = DP.runCardParts(run, EN, {}).map(([, h]) => h).join("");
    const again = DP.runCardParts(run, EN, {}).map(([, h]) => h).join("");
    assert.equal(later, again, "identical markup: nothing to patch, details and focus untouched");
    assert.ok(!later.includes("m-play"));
    assert.ok(first.length > 0);
  } finally { performance.now = origNow; }
});

/* ---------- T15: history and demo are static and local ---------- */
test("T15 opening a record or a demo makes no request and plays nothing", () => {
  let fetched = 0;
  globalThis.fetch = () => { fetched++; throw new Error("no network"); };
  const run = play(load("research-filter"), "hist-1");
  M.markStatic("hist-1");
  const html = DP.decisionsParts(run, EN, { source: "recorded" }).map(([, h]) => h).join("");
  assert.equal(fetched, 0);
  assert.ok(!html.includes("m-play") && !html.includes("m-fade"));
  assert.ok(html.includes("Recorded run"));
  const demo = DP.decisionsParts(play(load("feedback-zh"), "demo-1"), EN, { source: "demo" }).map(([, h]) => h).join("");
  assert.ok(demo.includes("Demo replay") && demo.includes("No model was called"));
  delete globalThis.fetch;
});

/* ---------- T16: motion settings change presentation only ---------- */
test("T16 Off, Reduced and background give the same state and no replay", () => {
  const events = load("needs-review");
  const snap = (r) => JSON.stringify({ s: r.status, b: r.batches, rv: r.reviews, rt: r.routing });
  M.setPreference("off");
  const a = play(events, "m1");
  assert.equal(M.claim("m1", "k", "p"), "");
  M.setPreference("reduced");
  const b = play(events, "m1");
  assert.equal(M.claim("m2", "k", "p"), "m-fade");
  M.setPreference("system");
  M._setHidden(true);
  const c = play(events, "m1");
  assert.equal(M.claim("m3", "k", "p"), "", "a background tab plays nothing");
  M._setHidden(false);
  assert.equal(M.claim("m3", "k", "p"), "", "and does not replay on return");
  assert.equal(snap(a), snap(b)); assert.equal(snap(b), snap(c));
});

/* ---------- T19: English interface, Chinese content ---------- */
test("T19 fixed labels follow the interface language; content keeps its own", () => {
  const run = play(load("feedback-zh"));
  const html = DP.decisionsParts(run, EN, { source: "demo" }).map(([, h]) => h).join("");
  assert.ok(html.includes("Completed"));
  assert.ok(html.includes("付款后页面一直转圈"), "the Chinese feedback is shown as written");
  assert.ok(!html.includes("已完成"), "no Chinese fixed label under an English interface");
  const zh = DP.decisionsParts(run, ZH, { source: "demo" }).map(([, h]) => h).join("");
  assert.ok(zh.includes("已完成"));
});

/* ---------- T20: nothing from content executes ---------- */
test("T20 titles and labels from content are escaped", () => {
  const run = RE.createRun("x");
  const evil = '<img src=x onerror="alert(1)">';
  RE.reduce(run, { ...ev(1, "evidence.filtered"), run_id: "x", payload: { round: 1, counts: { discovered: 1, not_assessed: 1 },
    items: [{ id: "src-1", title: evil, source: evil, status: "not_assessed", probability_yes: null }] } });
  RE.reduce(run, { ...ev(2, "batch.started"), run_id: "x", batch_id: "b", payload: { questions: [{ question_id: "q", type: "noul", label: evil, target: evil }] } });
  const html = DP.decisionsParts(run, EN, {}).map(([, h]) => h).join("") + DP.runCardParts(run, EN, {}).map(([, h]) => h).join("");
  assert.ok(!html.includes("<img"), "no markup from content survives");
  assert.ok(html.includes("&lt;img"));
});

/* ---------- B04: costs ---------- */
test("B04 unknown costs stay unknown; free is not zero calls", () => {
  const run = RE.createRun("r");
  RE.reduce(run, ev(1, "llm.completed", { actor: "llm", usage: { call_id: "a", cost_usd: 0, cost_source: "price_table_estimated" } }));
  RE.reduce(run, ev(2, "llm.failed", { actor: "llm", usage: { call_id: "b", cost_usd: null, cost_source: "unknown" } }));
  const t = RE.tally(run);
  assert.equal(t.unknownCalls, 1);
  assert.equal(t.freeAtRate, 1);
  const html = DP.costHTML(run, EN);
  assert.ok(html.includes("Unknown cost") && html.includes("Free at configured rate"));
});

/* ---------- M02 grouping ---------- */
test("M02 grouping by one Choice dimension re-reads the same results", () => {
  const run = play(load("feedback-zh"));
  const b = run.batches[run.batchOrder[0]];
  assert.equal(RE.choiceDimensions(b).length, 1);
  const grouped = DP.batchCardHTML(run, b, EN, { grouped: { [b.batch_id]: true } });
  assert.ok(grouped.includes("Bug report") && grouped.includes("Feature request"));
  assert.equal(Object.keys(run.calls).length, 1, "switching the view calls nothing");
});

/* ---------- the app sandbox ---------- */
const SB = await import(join(ui, "js", "sandbox.js"));
test("a generated app cannot call out, and the policy comes first", () => {
  const page = SB.withPolicy("<!doctype html><html><head><title>x</title></head><body>hi</body></html>", "http://127.0.0.1:8765");
  assert.ok(page.includes("connect-src 'none'") && page.includes("form-action 'none'"));
  assert.ok(page.indexOf("Content-Security-Policy") < page.indexOf("<title>"), "before anything can run");
  assert.ok(SB.APP_POLICY("http://x").includes("font-src http://x"), "the harness's own fonts still load");
  const bare = SB.withPolicy("<div>no head</div>");
  assert.ok(bare.startsWith("<meta http-equiv=\"Content-Security-Policy\""));
  const doc = SB.withPolicy("<!DOCTYPE html>\n<html><body>x</body></html>");
  assert.ok(doc.toLowerCase().startsWith("<!doctype html>"), "the doctype stays first");
  assert.ok(doc.includes("Content-Security-Policy"));
});

let failed = 0;
for (const [s, name, e] of results) {
  if (s === "ok") console.log(`  ok    ${name}`);
  else { failed++; console.log(`  FAIL  ${name}\n        ${e && e.message}`); }
}
console.log(`\n${results.length - failed}/${results.length} passed`);
process.exit(failed ? 1 : 0);
