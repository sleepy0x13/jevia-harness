# JEVia

A local harness that puts **Jev** — TypeSafe's System One model — in front of an
ordinary LLM, and only pays for the LLM when the work actually needs it.

> 中文产品介绍（功能、技术架构、设计思路）：[docs/product.md](docs/product.md)

Jev answers typed questions against a piece of material: classifications,
ratings, yes/no calls, each with a calibrated probability. It returns no text at
all, runs in a few hundred milliseconds, and costs about two hundredths of a
cent a call. An LLM writes prose. Most real tasks are mostly the first kind of
work wearing the second kind's clothes.

JEVia splits them apart:

```
  task
   ├─ plan            compiled once, then reused for the same instruction
   ├─ assess          one Jev call: look things up? several parts? an app?
   ├─ research        search ─► Jev picks what to open ─► fetch ─► Jev keeps
   │                  what bears on the question ─► the program builds what the
   │                  writer will receive ─► Jev says whether *that* is enough
   ├─ compose         if the answer is a screen: Jev picks the components and
   │                  the layout, a cheap model fills in the words (see below)
   ├─ route           ONE Jev call sizes every step, flags which need their own
   │                  lookup, and marks any the material already finishes
   ├─ work            each step is a TURN on the cheapest model that clears its
   │  │               own rung, in parallel when the steps are independent
   │  └─ step         one request and the tools it calls ─► results come back
   │                  ─► another request, until nothing is owed
   └─ assemble        joined under headings for free, or rewritten as one piece
                      if Jev says the seams would show
```

Every judgement in that diagram is Jev's, and each line of it is a single call
no matter how many results, sections or steps there are.

* **A step, then another.** The model that writes can also read a file, run a
  search, check its own change, and decide what to do next from the result.
  Offering it no tools makes a turn one request, which is what generation was
  before — one path, not two.
* **One call, many judgements.** Ten questions cost one round trip, not ten
  LLM calls.
* **The plan is compiled once.** The first run of an instruction costs a
  planning call; repeating it reuses the plan for free. The cache key keeps
  numbers, negations and order, so "3 cases" never borrows the plan for "30".
* **Jev sizes the model; a rule picks it.** Jev rates how much capability the
  step needs, and a price-table rule picks the cheapest model in your roster
  that clears that rung — or the strongest one you have, if you set Spending to
  *Best available*. The bottom rung means *no model at all* — the
  judgements were the answer. When nothing clears the rung the step waits for
  you (**No qualified model**) instead of running on something weaker — except
  for the safety margin the harness itself adds on a low-confidence rating,
  which is given up, and said so, rather than stopping the run.
* **You can see every judgement.** Each answer carries a compact run card —
  what stage it is in and who is working (Jev, a rule, a model, a tool) — and a
  Decisions view in the side panel with every batch, the raw answers, the rule
  that turned each one into an action, and the calls that were not made.
* **Uncertainty is spent on, not guessed through.** A decision too close to a
  coin flip is escalated instead of accepted.
* **You are answered in the language you asked in.** The interface toggle says
  what the buttons say; the reply follows the question, because somebody running
  the English interface and asking in Chinese wants a Chinese answer.

## The app factory

Ask for something to *use* rather than something to read — a booking page, a
tracker, a calculator, a dashboard — and JEVia builds a working app instead of
writing one.

It does not ask a model to write HTML. A model asked for a screen writes it token
by token, differently every time, and sometimes broken. Here the pieces already
exist: a catalogue of fourteen components (title, numbers, form, table, cards,
chart, search, checklist, timeline, tabs, progress, text, list, notice), each with
a fixed shape and a renderer that knows how to draw it. The work splits the way
everything else in JEVia does:

```
  assess   ──►  "is this a screen?"  rides in the call the run makes anyway
  compose  ──►  14 components × (use it? where?) + layout  ──►  1 Jev call
  layout   ──►  main or side panel, per component          ──►  1 more, only for split layouts
  fill     ──►  labels, rows, options as JSON, on the cheapest model that can
  render   ──►  one self-contained HTML file with its own small runtime
```

Everything a model wrote is escaped on the way into the page, so it cannot
inject markup; an unknown or malformed block is dropped rather than breaking the
page. The runtime makes the pieces work: forms validate and summarise, a search
box filters every table, card and list on the page, and a form's values narrow
the rows they match. The result opens live in the canvas — sandboxed with no
origin of its own, so it can run scripts but can never reach JEVia, its storage
or your keys — at desktop or phone width, and lands in the workspace as
`app.html`.

Some things cannot be assembled from standard parts — a game, an animation, a
drawing board. In the same compose call Jev also answers *parts, or code of its
own?*; for code, the model sized for code writes one self-contained page. The
reader never sees the code: the stream is read for its parts as it arrives —
the stylesheet, the canvas, `spawnFood`, `checkCollision`, the game loop — and
each is laid onto the board as it appears. Any ordinary answer that turns out to
contain a whole web page is treated the same way: the page is lifted out and run.

Measured: a Chinese request for a weekly workout tracker. Jev chose six
components in **518 ms for $0.00008**; the whole run, words included, cost
**$0.00011**. The **App** switch in the composer skips the question and always
builds one; Settings can turn apps off entirely.

## Skills

A folder of markdown files, each describing one kind of task and how to do it
properly. This is where out-of-the-box quality comes from: a model told "write a
briefing" produces something generic, and the same model handed three paragraphs
on what a good briefing contains produces something usable.

Eight ship with the harness — research brief, code review, customer reply,
summarise, compare options, extraction, plain writing, translation. Settings →
Skills lists them with a name and a one-line summary, opens any one for editing,
creates new ones, installs one from a link (a GitHub page or a raw `.md`,
including `name/SKILL.md` packs), and **uploads** a `.md` file or a `.zip` of
them. An upload is read, never run: a zip's own paths are ignored, each skill is
named from its own header, and anything that is not markdown is skipped and
reported. Headers written for other harnesses work too, including a multi-line
`description: |` block — the whole thing goes to Jev, and the list shows the
first line of it. What you save goes to `<workspace>/skills/`; saving over a
built-in keeps a copy that wins, and deleting the copy brings the original back.

Which one applies is a typed choice, so **Jev picks it inside the call the run
was already making** — no LLM, no extra round trip. A pick below 50% certainty
applies nothing, because forcing a shape onto a task that did not ask for it is
worse than leaving it alone.

## Roles

Each step of a task is staffed, not just prompted. A role carries a job
description, a floor on how capable its model must be, and whether it may go and
look things up. Jev picks the role per step, in the same routing call that sizes
the model.

| Role | Floor | Web | For |
| --- | --- | --- | --- |
| Extractor | 1 | — | Pulling named values out with no interpretation |
| Researcher | 2 | yes | Establishing facts and where they came from |
| Analyst | 3 | yes | Weighing evidence, reaching a judgement |
| Writer | 2 | — | Prose a reader will finish |
| Editor | 2 | — | Improving text that already exists |
| Engineer | 3 | — | Code, queries, configuration |

The floor matters: an analyst on the toy model produces confident nonsense, so a
low capability rating can be raised by the role but never lowered.

When none of the six fits, the planner writes a worker for the job instead — two
or three sentences saying what this one knows, always does, and refuses to do:

```json
{"title": "the regulatory annex",
 "persona": "You are a compliance officer who cites the clause number for every
             claim and refuses to paraphrase statute."}
```

That costs nothing extra. The planner was compiling this plan anyway, and the
plan is cached, so the bespoke worker is written once and reused for every task
of that shape. A step the plan staffed itself is not put to Jev again.

## Your work lands in your folder

The workspace is not a setting, it is where the work goes, so a run will not
start without one.

```
  <workspace>/chats/2026-09-19-triage-a1b2c3.md      the conversation, readable
  <workspace>/outputs/2026-09-19-triage-a1b2c3/      one file per step, answer.md,
                                                      evidence.md, app.html
  <workspace>/skills/                                 your own skills
  <workspace>/.jevia/                                 memory, plan cache, schedule
```

The transcript is markdown: the task, the material, every judgement with its
probability, which model wrote which step, the answer, the sources and what it
cost. Nothing needs this program to read it back.

## Memory

Facts the user states about themselves — a preference, a constraint, a name for
something in their world — are pulled out of their own words by pattern and
judged by Jev in the same call again. What survives goes into
`<workspace>/.jevia/memory.jsonl`, a plain text file you can read, edit or
delete, and into every later prompt.

Nothing is inferred and nothing is summarised. If you did not say it, it is not
in there.

## Tools, and who is allowed to use them

A turn can call tools, and what it may call is one setting — **Look only**,
**Read and write**, or **Also run commands** — not a list of checkboxes. On top
of that setting, every action with a side effect is judged by Jev before it
runs, and the tool's own rules apply after that: a path outside the workspace
is refused whatever the probability said. Switch on **Ask before each change**
and every write waits for you as well.

| | |
| --- | --- |
| Look only | `read_file` `list_dir` `search_files` `fetch_url` `web_search` `now` `calculate` `todo_write` `ask_user_question` `delegate` |
| Read and write | the above, plus `write_file` and `edit_file` (an exact passage, refused unless it appears once) |
| Also run commands | the above, plus `shell`, `run_code` and background `job_*`, and only when `JEVIA_ENABLE_SHELL=1` |

Two of those are worth naming. **`ask_user_question`** stops and asks rather
than guessing, and the run waits; a guess made early is a whole run spent on
the wrong thing. **`delegate`** hands one self-contained job to a model of its
own and gets back only the answer, which keeps a long search out of the
caller's context.

**Tools from elsewhere.** Settings → Tools connects [MCP](https://modelcontextprotocol.io)
servers, so anything already written for that protocol can be used here.
A server is a program that runs as you, so it is configured in
`<workspace>/.jevia/mcp.json` from that screen and nowhere else — a model
cannot conjure one — and its tools go past the same gate as the built-in ones.

**Plan first.** Switch on **Show the plan first** and nothing runs until you
have seen what it intends to do. Say what to change and it plans again with
that in hand.

## Watching the decisions

Every answer opens with a run card: a status bar (the stage, who is working —
**Jev**, **Policy**, **LLM** or **Tool** — and real elapsed time, never a made-up
percentage), the batch in flight, and a short decision summary. **View
decisions** opens the full record in the side panel:

* **Batches.** A batch of eight questions is shown as one call with eight
  answers: neutral placeholders while the request is out, then all eight settle
  together. A failed batch says it failed and why — it never turns into a No.
* **Three kinds of answer.** A yes/no shows `P(yes)` and the threshold it was
  read against. A choice shows the chosen option's own probability and a
  separate `Confidence`; the rest of the distribution is one click away and
  never renormalised. A score is drawn on its own scale, with the raw value and
  the level the policy adopted marked apart. A missing field says
  `Unavailable`, not 0.
* **Material.** Every source section is Kept, Needs review, Excluded or Not
  assessed, with counts that add up. Excluded means not sent to the writer;
  nothing is deleted.
* **Routing.** Jev's reading of each step (raw score, confidence, role) is
  shown apart from the step the price-table rule took (required level,
  eligible candidates, the pick). Candidates below the level say so.
* **What did not happen.** `Generation skipped` appears only when the decisions
  really are the deliverable, and `0 LLM calls` only when the whole run made
  none — after a planning call it is `No generation call for this step`.
* **Doubt, by name.** `No qualified model`, `Jev request failed` and
  `Low confidence` are different states. The first asks you to add a model or
  allow a labelled downgrade for that run.
* **Why this result** lists the rule and its inputs; nothing is written after
  the fact by a model to explain Jev.

Motion follows **Settings → Behaviour → Motion** (*Follow system*, *Reduced*,
*Off*), and the system's reduced-motion setting always wins. Motion never holds
anything back: the state is committed first, each change plays once, a
background tab plays nothing, and the results and calls are identical in every
setting. Stop asks the server to schedule nothing new and says **Cancel
requested** until it confirms; a request already with a provider may still be
billed, and the card says so.

Every run is recorded as structured events in `<workspace>/.jevia/runs/`
(scrubbed of keys, headers and cookies), so reopening an old answer shows the
same record — **Recorded run**, static, with no new calls. A run cut short by
closing the page comes back as **Interrupted**, not as finished. Settings →
Behaviour → Demo replay plays six scenarios built from test fixtures, always
labelled, never calling a model. The protocol and the delivery notes are in [docs/vNext.md](docs/vNext.md);
the agent loop and the tools are in [docs/agent-loop.md](docs/agent-loop.md).

## Research, without paying a frontier model to read

Ask something the material cannot answer and JEVia goes and looks. First the
cheapest model writes two or three real search queries — keywords, dated, one in
English when the subject is international — because nobody types "please look up
the flight prices for me" into a search box. They run at once, and the results'
own snippets count as evidence: for prices and scores they are often all there
is, since the pages behind them are booking forms that render nothing to a
fetch. A round that reads nothing tries other words instead of giving up. And if
the web truly gives nothing back, the writer is told so — it says what it
searched and invents no figures, rather than claiming it cannot browse.

The part worth explaining is who does the reading.

A search returns ten results. An ordinary agent hands all ten snippets to its
model and asks which to open, then hands it three full pages and asks what
matters. You pay for every word of that as input tokens.

Here, Jev is asked one yes/no per result — *would this help?* — and **all ten
travel in a single call**. The pages that survive are cut into sections and Jev
is asked one yes/no per section, again in one call (or a few, when the sections
do not fit one request's budget). Each section is then **Kept**, **Needs
review**, **Excluded** — or **Not assessed**, if it never reached Jev or its
answer was missing; a missing answer is never read as a no.

Only then is the question *is this enough?* asked — in its own call, about the
exact evidence the writer will receive, cumulative across rounds and cut to the
writer's budget. Asking it alongside the keep questions would judge material the
filter then drops: answers in one batch are independent.

```
  search  ──►  10 results    ──►  1 Jev call  ──►  3 opened
  fetch   ──►  26 sections   ──►  1 Jev call  ──►  17 kept, 2 to review, 7 excluded
  check   ──►  what is sent  ──►  1 Jev call  ──►  sufficient (P(yes) 0.92 ≥ 0.70)
```

An empty delivered set is never "enough", and a check that failed or came back
without an answer is **unknown**, not success. The loop stops for one stated
reason — sufficient, budget used, no new sources, error or cancelled — and
when it stops short the writer is told what is missing rather than left to
fill the gap.

## One task, several models

A task with parts is not written by one model in one go. The plan splits it into
steps, and **a single Jev call decides everything about all of them at once**:
how much capability each step needs, whether it needs its own lookup, whether
the material already finishes it, whether they can be written simultaneously,
and whether the finished pieces need rewriting into one voice.

Turning those judgements into model choices is arithmetic, so the harness does
it: each step gets the cheapest model in your roster that clears its own rung.
A reformatting step goes to the cheap model while the analysis beside it goes to
the expensive one — which is the entire reason to split a task up.

```
  route   ──►  3 steps × 3 questions + 2  ──►  1 Jev call
               reformat   → tier 1 → the cheap model
               analyse    → tier 4 → the strong model, own web lookup
               summarise  → already finished by the material → skipped
```

Steps that do not depend on each other run at the same time, so the wall clock
is the longest step rather than the sum. Each one is told the full outline and
which part is its own, so writing them out of order does not make them overlap.

A step is only skipped on a clear yes — omitting part of what was asked for is
far worse than writing it twice, so that threshold is deliberately high.

## Workers you can talk to

Every step of a multi-part task is a worker with its own role, model and log.
While it runs you watch it: what it searched, how many pages it read, which
model is writing, and the words themselves as they stream. Open any worker in
the side panel and speak to it — *make this shorter*, *add this year's figure* —
and it rewrites its own part with everything it had before: its brief, its
model, what the other workers wrote, and the conversation so far. Whether the
request needs a fresh lookup is Jev's call. When the answer was the parts joined
under headings, the rewritten part replaces the old one in place.

## Borrowed from the harness playbook

A few patterns every serious agent harness converges on, each implemented the
JEVia way — with Jev doing the judging where a typed question will do:

* **Conversation.** Follow-ups see the exchange so far: the last four turns
  verbatim, older ones trimmed, deterministically and without a summarising call.
* **House rules.** An `AGENTS.md` (or `JEVIA.md`) in the workspace is read into
  every prompt, the way coding agents read theirs.
* **A gate on tools with side effects.** Before a worker writes a file or fetches
  a URL, Jev is asked two questions about every pending action at once. Did your
  own instruction ask for it, and is it safe to run. They are asked apart because
  they fail apart: an action you named but that reaches outside the workspace is
  refused as unsafe, and one the fetched material slipped in is refused as
  something you never asked for. The action's destination is always shown to the
  gate in full, whatever it is carrying. Needs 0.8 on the first and 0.5 on the
  second; a refusal tells the worker which, and the same call is not judged twice.
* **Self-verification.** Jev checks every answer against the task. A miss gets
  exactly one more attempt, not a loop.
* **Evidence on disk.** Whatever research kept is written to `evidence.md` beside
  the answer, so the answer can be checked later without re-running anything.

## Providers

Bring a key for whatever you use: OpenRouter, OpenAI, Anthropic, Kimi, DeepSeek,
Gemini, GLM, MiniMax, Qwen — or any OpenAI-compatible endpoint by URL. Each key
is tested when added and lists what it can run; prices come from the vendor or,
failing that, OpenRouter's public list. Jev itself runs through **OpenRouter or
TypeSafe**, so one of those two keys is needed for the judging; the writing can
happen anywhere. Keys stay in your browser and are sent only with requests to
the vendor they belong to.

## Run it

```bash
python3 app.py
```

Open <http://127.0.0.1:8765>. The first run asks for an OpenRouter key (or
another provider's, from Settings → Keys) and a workspace folder, which you can
create and name from the picker. Nothing to install: Python 3.9 or newer,
standard library only.

It works like any chat tool. Type what you want done, attach material with **+**,
send. Type `/` for skills and commands, filtered as you type. What Jev decided
is shown in plain words above the answer, with the full
distributions one click away; the steps, which model did each, the sources and a
ledger of what it cost sit underneath. Several tasks can run at once; a message
sent to a busy task queues behind it, and ■ stops it. Outputs and apps open in
the canvas on the right; both side panels resize by dragging their edge.

The interface is English and Chinese, switched in the sidebar, and set in the
Beating editorial manner — paper white, whole fields of blue, hairlines, a
single asterisk, and motion that reveals and then rests. Generated apps wear the
same clothes. Settings → Behaviour has one choice between thrifty, balanced and
careful, with raw thresholds under Advanced.

For local development you can put a key in `.env` instead:

```bash
cp .env.example .env    # then edit it
```

`.env` is gitignored. If you have ever pasted a key into a chat, a terminal you
share, or a screenshot, revoke it and make a new one.

## Use it from Python

```python
from jevharness.harness import Harness, Settings, parse_task
from jevharness.questions import Choice, Noul, Score

harness = Harness(Settings.from_env())

# Typed decisions: one Jev call, never an LLM.
result = harness.decide(ticket_text, {
    "department": Choice(
        instructions="Which team should handle this",
        options={"billing": "Payments and refunds",
                 "technical": "Bugs and integration failures",
                 "sales": "Pricing and accounts"},
    ),
    "severity": Score(
        instructions="How severe is this for the customer",
        levels=["Minor", "Real problem", "Blocking, money involved",
                "Legal or chargeback risk"],
    ),
    "refund_requested": Noul(instructions="Is the customer asking for a refund?"),
})

if result.decisions["severity"].value >= 2.5:
    page_the_retention_queue()
```

A natural-language task instead, planned and gated automatically:

```python
result = harness.run(parse_task({
    "prompt": "Triage this ticket and draft a reply only if one is warranted.",
    "state": ticket_text,
}))
print(result.output)
print(result.generation_skipped, result.ledger.total_cost)
```

`examples/use_as_a_library.py` runs all three entry points against real APIs.

## HTTP API

| Endpoint | What it does |
| --- | --- |
| `POST /api/run` | Full pipeline, streamed as server-sent events; takes a `run_id` |
| `POST /api/runs` | Cancel a run in flight; list, read or export recorded runs |
| `POST /api/sync` | Rewrite a whole marked Needs sync from its current parts |
| `POST /api/decide` | Typed questions only — one Jev call, guaranteed no LLM |
| `POST /api/plan` | Decompose the task without answering or generating |
| `POST /api/models` | What one key can run, priced where known |
| `POST /api/skills` | List, read, save, install or delete skills |
| `POST /api/schedule` | Create, list, pause, run or remove schedules and loops |
| `POST /api/memory` | Read, add, forget or clear what JEVia remembers |
| `POST /api/workspace` | Browse folders and create one |
| `GET /api/catalogue` | Providers, built-in skills and roles |
| `GET /api/health` | Liveness, and whether a `.env` key is present |

Every request carries its own `settings`, so the server holds no credentials of
its own and one instance can serve several people with separate keys. A POST
from another site (by `Origin` or `Sec-Fetch-Site`) is refused.

```bash
curl -s localhost:8765/api/decide -H 'content-type: application/json' -d '{
  "settings": {"roster": {"credentials": [{"ref": "k", "api_key": "sk-or-v1-..."}],
                          "models": [{"id": "m", "model": "qwen/qwen3.7-flash",
                                      "capability": 3, "credential_ref": "k"}]}},
  "task": {"prompt": "triage", "state": "the ticket text",
           "questions": {"urgent": {"type": "noul",
                                    "instructions": "Does this need attention today?"}}}
}'
```

## How the pieces fit

| File | Responsibility |
| --- | --- |
| `questions.py` | Jev's three primitives and their answers, validated locally |
| `appkit.py` | The app factory: component catalogue, composer, filler, renderer |
| `vendors.py` | Every provider's endpoint, dialect, key format and price source |
| `research.py` | Search, fetch and sectioning — candidates only, no judging |
| `agent.py` | The research loop and the step router, with Jev as the filter |
| `loop.py` | The agent loop: turns, steps, the gate, and the budgets |
| `session.py` | The log a step is built from; what the model sees is what it says |
| `mcp.py` | Tools from elsewhere: an MCP client, over stdio JSON-RPC |
| `tools.py` | The tool registry: clock, arithmetic, files, fetch, opt-in shell |
| `skills.py` | Markdown instruction packs: built-in, written, installed or uploaded |
| `roles.py` | Who does each step, and the floor under their model |
| `memory.py` | What the user has told you about themselves |
| `scheduler.py` | Tasks that run without a browser open |
| `store.py` | Transcripts and outputs, written into the workspace |
| `plan.py` | The task, the plan, and the gate conditions |
| `roster.py` | Your models and keys, and the cheapest-that-fits selector |
| `planner.py` | Task → plan: declared, recognised, cached, or compiled |
| `policy.py` | Pure rules for what counts as uncertain, and the prompts |
| `executor.py` | Runs a plan: one Jev call, then only what survives the gate |
| `ledger.py` | Costs by call id and cost source, and the folded counterfactual |
| `events.py` | The decision-event protocol: one outlet per run, cancel, record |
| `redact.py` | Scrubs keys, headers and cookies from anything logged or streamed |
| `goals.py` | Loop goals: countable conditions checked by code, the rest by Jev |
| `evaluation.py` | P2 placeholders: a comparison record format and interfaces |
| `harness.py` | Wiring and credentials — the only module that sees a key |
| `server.py` | Local HTTP, SSE, and the static UI |
| `ui/` | `index.html`, one stylesheet, and ES modules: `core` (state, API, markdown), `chat`, `sidebar`, `workspace`, `prefs`, `canvas`, `copy` (both languages), `run-events` (the event reducer), `decision-panel` (run card and Decisions view), `motion` (play-once and the motion setting); `fixtures/` for Demo replay |

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

435 tests, no network and no key required — the engines and the web are faked
at the transport, so decomposition, research filtering, step routing, parallel
execution, assembly, gating, model choice, escalation, app composition and
rendering, the host guard, the decision-event protocol, cancelling and the
accounting are all checked offline. `tests/test_vnext.py` holds the vNext
regression cases T01–T20.

```bash
node scripts/ui_tests.mjs     # the event reducer, motion, rendering and the app sandbox
node scripts/copy_audit.mjs   # English and Chinese define the same words
```

To work on the interface without spending anything:

```bash
python3 scripts/offline_server.py      # http://127.0.0.1:8766, every engine faked
python3 scripts/make_fixtures.py       # rebuilds the Demo replay fixtures, offline
```

## What it costs

Measured on a support-triage task with six judgements and a drafted reply:

| | |
| --- | --- |
| Jev, 6 judgements + model sizing | one call, 536 ms, $0.000033 |
| Reply, on the free model it picked | 8.6 s, $0 |
| The same task sent straight to the best model | $0.000243 |

A second measurement, on a review-handling task where the gate fired: the whole
run finished in 584 ms and made no LLM call at all, because Jev judged the
review needed no reply.

A third, on a briefing written from the open web — search, filter 74 sections
down to 24, route three steps, write them in parallel on the model each one
needed, join them: **$0.00074 in total**, against $0.0016 for the same work sent
straight to the strongest model in the roster.

These are single past measurements, not a promise. The interface shows what was
measured and nothing else: the known cost, how many requests had no known cost
(a total with unknowns in it is a floor), the calls to each engine, and the
time. There is no "you saved N×" figure and no modelled comparison. Each
charged request is counted once by its call id, and its cost says where it came
from — reported by the provider, estimated from the configured price table, or
unknown. A free model is "free at configured rate"; that is not the same as no
call having been made.

## Tools

A small registry ships enabled: the clock, exact arithmetic over a parsed AST
(never `eval`), and read-only workspace access — read a file, list a directory,
search for a phrase. `write_file` and `fetch_url` are available but off until
you turn them on.

File tools are confined to one workspace directory (`JEVIA_WORKSPACE`, default
`./workspace`), with symlinks resolved before the containment check. The fetch
tool refuses anything but http/https and refuses hosts that resolve into private
or loopback space.

There is a `shell` tool. It is off unless you set `JEVIA_ENABLE_SHELL=1`, it
runs in the workspace with a timeout, and it is not a sandbox — it is a shell.
Turn it on only if you would have run the command yourself.

Behind a proxy, a container network or a corporate DNS, every hostname resolves
to a private address and the fetch guard rejects the entire web. Set
`JEVIA_ALLOW_PRIVATE_HOSTS=1` to skip the resolution check; the name rules
(localhost, `.internal`, cloud metadata addresses, literal private IPs) keep
applying either way.

## Schedules and loops

A task can run on an interval without the browser open. Give it a goal — *until
the draft is under 200 words and has a call to action* — and it becomes a loop:
each round is fed the previous round's output and the goal is checked after
every round. Countable conditions — *under 200 words*, *280字以内*, *must include
"refund"* — are checked by code, with the counting rule stated; whatever is left
is one question for Jev (met at 0.75 or above). The loop stops itself when every
condition passes (**Goal met**) or at the round limit (**Limit reached**, which
is not the same thing). Settings → Loops shows every round and every condition
with who checked it.

Either kind needs a workspace, because the job has to be stored somewhere, and
it stores the settings it will run under — **including your API key** — in
`<workspace>/.jevia/schedule.json` with owner-only permissions. There is no way round that: at 3am there is no
browser to ask. If you would rather not have a key on disk, do not schedule
anything; everything else works without one.

A job that throws is recorded and disabled rather than retried into a hole.

## Notes on the engines

Thinking is disabled by default on every generation call. Several cheap models
are reasoning models that will spend an entire token budget thinking and return
an empty message; measured on `qwen/qwen3.7-flash`, the same one-word answer
cost nine times more with it on. Turn it back on per model in the roster if a
particular model earns it.

Jev is reached through OpenRouter's `/api/alpha/decisions` endpoint as
`~typesafe/jev-latest`. It is not a chat model and the chat endpoint rejects it.

## Licences

The bundled fonts, Space Grotesk and Instrument Serif, are under the SIL Open
Font License 1.1. The
harness itself is MIT.
