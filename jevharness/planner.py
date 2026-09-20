"""Task -> Plan, by the cheapest route that works.

Four sources, tried in order, and the first three are free:

1. **declared**      the caller sent typed questions, so the plan is already known
2. **deterministic** the task's shape is recognisable locally (options listed in
                     the prompt, a rating request, a yes/no question, or an
                     unmistakable writing job)
3. **cached**        this task *shape* was compiled earlier; the state differs
                     but the questions do not
4. **compiled**      one LLM call writes the plan, which is then cached forever

Only step 4 costs money, and it is paid once per task shape. Every later task
of that shape is Jev-only unless it genuinely needs prose. That is the whole
economic argument for the harness: the LLM compiles, Jev executes.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import unicodedata
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .errors import CompileError, SchemaError
from .plan import (
    Research,
    FROM_DECISIONS,
    FROM_GENERATION,
    SOURCE_CACHED,
    SOURCE_COMPILED,
    SOURCE_DECLARED,
    SOURCE_DETERMINISTIC,
    SOURCE_FALLBACK,
    Condition,
    Gate,
    Generation,
    Plan,
    Task,
)
from .providers import Call, LLMClient
from .questions import Choice, Noul, Score

# --------------------------------------------------------------------------- #
# Signals. Bilingual, because the harness is used in both languages.
# --------------------------------------------------------------------------- #

GENERATION_VERBS = re.compile(
    r"\b(write|draft|compose|rewrite|rephrase|summari[sz]e|translate|explain|"
    r"describe|outline|brainstorm|generate|implement|refactor|fix|debug|code|"
    r"design|plan|email|reply|respond|essay|article|story|poem|script)\b"
    r"|写|撰写|起草|改写|重写|润色|翻译|解释|说明|总结|概括|生成|实现|重构|"
    r"修复|调试|设计|草拟|回复|回信|文案|文章|大纲|方案",
    re.IGNORECASE,
)

CLASSIFY_VERBS = re.compile(
    r"\b(classify|categor(?:y|ise|ize)|label|route|triage|tag|assign|sort into|"
    r"which (?:one|of|team|category|bucket|queue))\b"
    r"|分类|归类|归为|分为|分成|分到|打标签|路由|分派|分流|派给|归到|属于哪",
    re.IGNORECASE,
)

RATING_VERBS = re.compile(
    r"\b(rate|score|rank|grade|severity|priority|how (?:severe|urgent|risky|"
    r"likely|confident|good|bad))\b|评分|打分|评级|分级|严重程度|优先级|风险等级",
    re.IGNORECASE,
)

# Words that betray a judgement hiding inside what looks like a writing job:
# "draft a reply IF one is warranted" is two steps, not one. When any of these
# appear, the fast generation-only path is refused and the task goes to the
# compiler, which can split the decision out and let the gate skip the LLM.
CONDITIONAL = re.compile(
    r"\b(if|whether|unless|only when|only if|depending|as (?:needed|appropriate|warranted)|"
    r"warranted|appropriate|decide|determine|work out|figure out|assess|evaluate|"
    r"triage|when necessary|if necessary|otherwise)\b"
    r"|如果|是否|要不要|该不该|判断|决定|评估|视情况|必要时|酌情|看情况",
    re.IGNORECASE,
)

# A link in the task is an instruction to go and read it.
URL_IN_PROMPT = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)

# Asking for something the material cannot contain.
LOOKUP = re.compile(
    r"\b(search|look ?up|google|find out|latest|current|today'?s|recent|news|"
    r"who is|what is the price|how much does|release[ds]? (?:in|on)|as of)\b"
    r"|搜索|搜一下|查一下|查查|最新|近期|现在的|目前的|今天的|新闻|行情|价格是多少",
    re.IGNORECASE,
)

# A deliverable the task itself says is tiny. Splitting "one sentence" into
# three steps costs three calls and produces something longer than was asked.
TINY = re.compile(
    r"\b(?:one|two|three|a single|1|2|3)[ -](?:sentence|line|word|paragraph)s?\b"
    r"|\b(?:a|one)(?: single)? (?:title|headline|tweet|tagline|slogan|subject line)\b"
    r"|[一二三两1-3]\s*(?:句|行|段)(?:话)?|一个(?:标题|口号|标语)|一条(?:推文|微博)",
    re.IGNORECASE,
)

# Words that say the deliverable has parts.
MULTIPART = re.compile(
    r"\b(and then|as well as|sections?|parts?|compare|contrast|pros and cons|"
    r"outline|report|analysis|breakdown|step by step|each of)\b"
    r"|以及|并且|分别|几部分|几点|对比|优缺点|报告|分析|拆解|逐条|每个"
    r"|然后|再|接着|之后|最后|首先|提纲|大纲|梳理|整理|全面|详细|深度|调研|方面"
    r"|分[为成]?[一二三四五六七八九十两\d]+(?:个)?(?:部分|点|节|步|章)",
    re.IGNORECASE,
)

YESNO_LEAD = re.compile(
    r"^\s*(is|are|was|were|does|do|did|can|could|should|would|will|has|have|had)\b",
    re.IGNORECASE,
)
YESNO_ZH = re.compile(r"是否|有没有|是不是|会不会|能不能|可不可以|算不算|该不该")

# "into A, B or C" / "one of: A / B / C" / "分类到 A、B、C"
# Longest alternatives first, so "分类到" is consumed whole and its trailing
# character does not end up glued to the first option.
OPTION_LIST = re.compile(
    r"(?:into|among|between|one of|from|options?(?: are)?|choose from|"
    r"分类到|分类成|分类为|归类到|归类为|分到|分成|归到|分类|归类|其中之一)"
    r"\s*[:：]?\s*(?P<body>[^.?!\n；。]{3,240})",
    re.IGNORECASE,
)
OPTION_SPLIT = re.compile(r"\s*(?:,|/|\||、|，|;|；|\bor\b|\band\b|或|和)\s*")


def _extract_options(prompt: str) -> List[str]:
    """Pull an explicitly enumerated option set out of the prompt."""
    for match in OPTION_LIST.finditer(prompt):
        body = match.group("body").strip().strip("\"'`")
        parts = [p.strip().strip("\"'`.") for p in OPTION_SPLIT.split(body)]
        parts = [p for p in parts if 0 < len(p) <= 48]
        # Deduplicate while preserving the author's order.
        seen: Dict[str, None] = OrderedDict()
        for part in parts:
            seen.setdefault(part, None)
        options = list(seen)
        if 2 <= len(options) <= 24:
            return options
    return []


def _slug(text: str, fallback: str) -> str:
    """An option id a human will recognise in the result.

    Latin text becomes a snake_case slug. Anything else — Chinese, Japanese,
    Cyrillic — keeps its own characters, because "option_1" tells the reader
    nothing and the API accepts arbitrary strings.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    if slug:
        return slug
    cleaned = re.sub(r"\s+", "", text).strip("：:，,。.、")
    return cleaned[:40] or fallback


# --------------------------------------------------------------------------- #
# Plan cache
# --------------------------------------------------------------------------- #


# Bump when the key's recipe or the planner's behaviour changes: every plan
# stored under another version is dropped on load rather than reused.
CACHE_VERSION = "plans/v2"


def normalise_instruction(text: str) -> str:
    """The instruction with only meaningless differences removed.

    Width, case and runs of whitespace go; words, numbers, negations and
    their order all stay. "3 cases" and "30 cases" are different tasks, and
    so are "English to Chinese" and "Chinese to English".
    """
    text = unicodedata.normalize("NFKC", text or "")
    text = " ".join(text.split()).lower()
    return text.rstrip(" .。!！?？")


def plan_cache_key(task: Task, *, language: str, skill=None, must_compile: bool = False) -> str:
    """A conservative key: reuse a plan only for the same instruction in the same setting.

    It covers everything the compiler sees that could change the plan — the
    whole normalised instruction, the skill's actual text, the planner's own
    prompt, the conversation it continues, whether material came with it —
    and nothing else. No word-set similarity: a key that drops numbers or
    order hands one task another task's plan.
    """
    skill_text = ""
    if skill is not None:
        skill_text = f"{getattr(skill, 'id', '')}\n{getattr(skill, 'body', '')}"
    parts = {
        "v": CACHE_VERSION,
        "planner": hashlib.sha256(COMPILER_SYSTEM.encode("utf-8")).hexdigest()[:16],
        "language": language,
        "skill": hashlib.sha256(skill_text.encode("utf-8")).hexdigest()[:16] if skill_text else "-",
        "instruction": normalise_instruction(task.prompt),
        "declared": sorted((task.questions or {}).keys()),
        "conversation": hashlib.sha256(task.conversation(1500).encode("utf-8")).hexdigest()[:16]
        if task.history else "-",
        "material": bool(task.state_text.strip()),
        "must_compile": bool(must_compile),
    }
    digest = hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True)
                            .encode("utf-8")).hexdigest()[:32]
    return f"{CACHE_VERSION}:{digest}"


class PlanCache:
    """Thread-safe LRU that survives a restart.

    Without the file, every restart re-buys plans the user already paid for —
    which is most of the cost of the harness on a normal day. Plans only:
    research is never cached, so yesterday's evidence cannot turn up as the
    source for today's answer.
    """

    FILE = Path(".jevia") / "plans.json"

    def __init__(self, capacity: int = 512, workspace=None) -> None:
        self._capacity = max(1, capacity)
        self._store: "OrderedDict[str, Plan]" = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.path = (Path(workspace) / self.FILE) if workspace else None
        # Plans dropped on load because they were stored under an older key.
        self.discarded = 0
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.is_file():
            return
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(stored, dict):
            return
        if stored.get("version") != CACHE_VERSION or not isinstance(stored.get("plans"), dict):
            # An older file keyed plans by a word set; none of it is safe to
            # reuse. It is replaced on the next save.
            self.discarded = len(stored.get("plans") or stored) if isinstance(stored, dict) else 0
            return
        rows = stored["plans"]
        for key, raw in list(rows.items())[-self._capacity:]:
            if not str(key).startswith(CACHE_VERSION + ":"):
                self.discarded += 1
                continue
            try:
                self._store[key] = Plan.from_dict(raw, source=SOURCE_CACHED)
            except (SchemaError, TypeError, ValueError):
                continue

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                rows = {k: v.to_dict() for k, v in self._store.items()}
            self.path.write_text(json.dumps({"version": CACHE_VERSION, "plans": rows},
                                            ensure_ascii=False), encoding="utf-8")
        except OSError:
            return

    def get(self, key: str) -> Optional[Plan]:
        with self._lock:
            plan = self._store.get(key)
            if plan is None:
                self.misses += 1
                return None
            self._store.move_to_end(key)
            self.hits += 1
            return plan

    def put(self, key: str, plan: Plan) -> None:
        with self._lock:
            self._store[key] = plan
            self._store.move_to_end(key)
            while len(self._store) > self._capacity:
                self._store.popitem(last=False)
        self._save()

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
        self._save()

    def stats(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {
                "size": len(self._store),
                "hits": self.hits,
                "misses": self.misses,
                "rate": round(self.hits / total, 3) if total else None,
                "persisted": bool(self.path),
                "version": CACHE_VERSION,
                "discarded": self.discarded,
            }


# --------------------------------------------------------------------------- #
# The compiler prompt
# --------------------------------------------------------------------------- #

COMPILER_SYSTEM = """\
You are a planner for a two-engine harness. Keep the plan short: at most 5 steps \
and 6 decisions, every "instructions" under 25 words. Return JSON only — no prose, no \
code fences.

Engine one is Jev, a System One model. It answers typed questions against a \
state, all of them in parallel in a single call, and returns calibrated \
probabilities. It cannot write text. Its three question types are:
  choice  {"type":"choice","instructions":"...","criteria":{"option_id":"what it means", ...}}
  score   {"type":"score","instructions":"...","criteria":["lowest level","...","highest level"]}
  noul    {"type":"noul","instructions":"a yes/no question"}

Engine two is an ordinary LLM. It writes prose and code, and it costs far more.

Your job is to decompose the task so that as much as possible is settled by \
typed questions, because every question you write is answered in the SAME one \
Jev call at effectively no cost. Extract EVERY independent judgement the task \
requires as its own question. Do not collapse three judgements into one.

The harness can also search the web and read pages before any of this runs.
Search results and page sections are filtered by Jev, so asking for research is
cheap; asking for it when the material already answers the task is waste.

Return exactly this shape:
{
  "strategy": "three or four words naming the approach",
  "answer_from": "decisions" | "generation",
  "research": {"rounds": 1|2, "query": "the search query to start from", "top_k": 1-5} | null,
  "steps": [{"title": "one part of the deliverable",
             "role": "extractor"|"researcher"|"analyst"|"writer"|"editor"|"coder",
             "persona": "only when no role above fits"}, ...],
  "decisions": { "snake_case_name": <question>, ... },
  "gate": {"skip_generation_when": [{"decision":"name","op":"yes"|"no"|"is"|"at_least"|"below","value":<option id or number>}]},
  "generation": {"instruction":"what the LLM must write, in the imperative","include_state":true|false} | null,
  "notes": "one sentence on why this split"
}

Rules:
- "answer_from":"decisions" when the typed answers ARE the deliverable and the \
task asks for nothing to be written. Then "generation" must be null.
- If the task asks for ANY text to be produced — a reply, a summary, a note, a \
draft — then "answer_from" MUST be "generation" and "generation" MUST NOT be \
null, even when the task only wants it in some cases ("reply if warranted", \
"回信才回信"). Express the condition in "gate" instead; that is exactly what \
the gate is for. A plan that cannot write leaves such a task half done.
- Still write decisions for every judgement the writing depends on — they make \
the prompt shorter and let the harness pick a cheaper model.
- Use "gate" to name conditions that make generation pointless, so the \
expensive call is skipped.
- Question names must be snake_case and must not begin with an underscore.
- "score" needs at least two ordered level descriptions; "choice" at least two \
options. Describe each option and level concretely.
- For anything yes/no, use "noul". Never write a "choice" whose options are \
yes and no — noul returns a calibrated probability and the gate reads it \
directly.
- Never ask Jev to produce text, and never ask it to do arithmetic.
- Set "research" when the answer depends on facts the material cannot contain: \
current events, prices, documentation, anything about the world after your \
training. Write "query" as search keywords, not as a sentence. Otherwise null.
- "steps" splits the deliverable into parts that are written separately. Use 2 \
to 6 of them whenever the task has distinct parts — sections of a document, \
stages of an analysis, several questions in one ask. Each step is written on \
its own, by whichever model is cheap enough to handle that part, and steps the \
material already answers are skipped entirely. Name what each part *is* \
("the pricing comparison", "the risks section"), not what to do about it. Use \
[] only when the answer is genuinely one short piece of writing. Never make a \
step out of searching, and never make one out of listing sources or citations — \
the harness does both itself and a step for either is wasted money.
- Staff each step. Give "role" when one of the six fits: extractor (copies \
values out), researcher (establishes facts with sources), analyst (weighs and \
judges), writer (prose), editor (improves existing text), coder (code and \
config). When the step needs expertise none of them describes, leave "role" out \
and write "persona" instead: two or three sentences in the second person saying \
what this worker knows, what they always do, and what they refuse to do. Write a \
persona only when it would genuinely change the output — a generic one is worse \
than the role it replaced.
"""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _first_json_object(text: str) -> str:
    """Find the outermost balanced {...}, tolerating chatter around it."""
    text = _strip_fences(text)
    start = text.find("{")
    if start < 0:
        raise CompileError("planner returned no JSON object")
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise CompileError("planner returned an unterminated JSON object")


class Planner:
    """Produces a Plan for a Task, preferring the free routes."""

    LANGUAGES = {"zh": "Simplified Chinese", "en": "English"}

    def __init__(self, llm: Optional[LLMClient] = None, *,
                 cache: Optional[PlanCache] = None,
                 compiler_model: Optional[str] = None,
                 language: str = "en",
                 skill=None,
                 backup: Optional[Tuple[LLMClient, str]] = None) -> None:
        # Set per run when Jev has said the task has several stages.
        self.must_compile = False
        # What the user said when they sent a proposed plan back.
        self.feedback = ""
        self.llm = llm
        self.cache = cache if cache is not None else PlanCache()
        self.compiler_model = compiler_model
        # A stronger model to try once when the cheap one's plan does not parse.
        self.backup = backup
        self.language = language if language in self.LANGUAGES else "en"
        self.skill = skill

    # -- public ------------------------------------------------------------- #

    def plan(self, task: Task) -> Tuple[Plan, Optional[Call]]:
        """Return a validated plan and the compile call, if one was needed."""
        task.validate()

        declared = self._from_declared(task)
        if declared is not None:
            return declared, None

        # The shortcuts are for single pieces of work. When Jev has said the
        # task has several stages, it goes to the compiler to be split.
        recognized = None if self.must_compile else self._from_signals(task)
        if recognized is not None:
            return recognized, None

        key = plan_cache_key(task, language=self.language, skill=self.skill,
                             must_compile=self.must_compile)
        # A plan the user sent back is compiled again with what they said, and
        # never served from, or written to, the cache under the old key.
        cached = None if self.feedback else self.cache.get(key)
        if cached is not None:
            return _retagged(cached, SOURCE_CACHED), None

        if self.llm is None:
            return self._fallback(task, "no planner model is configured"), None
        if self.feedback:
            key = f"{key}:{hashlib.sha256(self.feedback.encode('utf-8')).hexdigest()[:8]}"

        try:
            plan, call = self._compile(task)
        except (CompileError, SchemaError) as exc:
            # A cheap model that runs out of room mid-plan is the usual cause.
            # One more attempt on a stronger model beats giving up on steps.
            if self.backup is None:
                return self._fallback(task, str(exc)), None
            try:
                plan, call = self._compile(task, *self.backup)
            except (CompileError, SchemaError) as again:
                return self._fallback(task, f"{exc}; then {again}"), None
        self.cache.put(key, plan)
        return plan, call

    # -- 1. declared -------------------------------------------------------- #

    def _from_declared(self, task: Task) -> Optional[Plan]:
        if not task.questions:
            return None
        plan = Plan(
            strategy="caller-declared decisions",
            answer_from=FROM_DECISIONS,
            decisions=dict(task.questions),
            source=SOURCE_DECLARED,
            strategy_code="declared",
            notes="The caller supplied a typed schema, so no planning was needed.",
        )
        plan.validate()
        return plan

    # -- 2. deterministic --------------------------------------------------- #

    def _from_signals(self, task: Task) -> Optional[Plan]:
        prompt = task.prompt.strip()
        wants_text = bool(GENERATION_VERBS.search(prompt))

        # A task that names a link, or asks for something the material cannot
        # hold, is a research task. Recognising that here costs nothing.
        urls = URL_IN_PROMPT.findall(prompt)
        if urls:
            return self._research_plan(task, Research(rounds=1, urls=tuple(urls[:4])))
        # Only a short, single lookup skips planning; anything with parts is
        # compiled, with the research folded into the plan.
        if (LOOKUP.search(prompt) and not task.state_text.strip()
                and len(prompt) <= 60 and not MULTIPART.search(prompt)):
            return self._research_plan(task, Research(rounds=2, top_k=3))

        options = _extract_options(prompt)
        if options and not wants_text and CLASSIFY_VERBS.search(prompt):
            name = _slug(_leading_noun(prompt), "category")
            return _single_decision_plan(
                name,
                Choice(
                    instructions=prompt,
                    options={_slug(o, f"option_{i}"): o for i, o in enumerate(options)},
                ),
                "options enumerated in the prompt",
            )

        if RATING_VERBS.search(prompt) and not wants_text:
            levels = _rating_levels(prompt)
            if levels:
                return _single_decision_plan(
                    "rating",
                    Score(instructions=prompt, levels=levels),
                    "a rating request with no text to write",
                )

        is_yesno = bool(YESNO_LEAD.match(prompt) or YESNO_ZH.search(prompt))
        if is_yesno and not wants_text and prompt.endswith(("?", "？")):
            return _single_decision_plan(
                "answer",
                Noul(instructions=prompt),
                "a bare yes/no question",
            )

        # An unmistakable writing job with nothing to decide: skip planning and
        # let the roster pick a model. Compiling a plan here would cost money to
        # learn what the verb already told us.
        #
        # This path must stay narrow. Anything conditional ("reply if one is
        # warranted"), anything that asks for a judgement, and anything long
        # enough to hide a second step is handed to the compiler instead — that
        # is where a decision gets split out and the LLM call can be skipped.
        # Only a short, single, material-bound instruction skips planning. A
        # longer ask, or one naming several parts, goes to the compiler so it
        # can be split into steps and researched.
        plainly_writing = (
            wants_text
            and not CONDITIONAL.search(prompt)
            and not LOOKUP.search(prompt)
            and not (options or CLASSIFY_VERBS.search(prompt)
                     or RATING_VERBS.search(prompt) or is_yesno)
            and len(prompt) <= 120
            and not MULTIPART.search(prompt)
        )
        if plainly_writing:
            plan = Plan(
                strategy="generation only",
                answer_from=FROM_GENERATION,
                generation=Generation(instruction=prompt, include_state=bool(task.state_text)),
                source=SOURCE_DETERMINISTIC,
                strategy_code="generation_only",
                notes="The task is plainly a writing job; Jev still sizes the model.",
            )
            plan.validate()
            return plan

        return None

    def _research_plan(self, task: Task, spec: Research) -> Plan:
        plan = Plan(
            strategy="look it up, then write",
            answer_from=FROM_GENERATION,
            generation=Generation(instruction=task.prompt.strip(), include_state=True),
            research=spec,
            source=SOURCE_DETERMINISTIC,
            strategy_code="research",
            notes="Recognised locally: the task points outside the material.",
        )
        plan.validate()
        return plan

    # -- 4. compiled -------------------------------------------------------- #

    def _compile(self, task: Task, llm: Optional[LLMClient] = None,
                 model: Optional[str] = None) -> Tuple[Plan, Call]:
        llm, model = llm or self.llm, model or self.compiler_model
        assert llm is not None
        state_preview = task.state_text.strip()
        if len(state_preview) > 1200:
            state_preview = state_preview[:1200] + "\n...[truncated for planning]"
        guidance = ""
        if self.skill is not None:
            guidance = (
                f"HOUSE INSTRUCTIONS for this kind of task ({self.skill.name}). "
                "Shape the steps and the decisions so the work below can be done "
                f"properly:\n{self.skill.body}\n\n"
            )
        user = (
            guidance
            + f"Write \"strategy\" and \"notes\" in "
            f"{self.LANGUAGES[self.language]}. Keep question names in snake_case "
            "English; write every \"instructions\" and \"criteria\" value in the "
            "same language as the task itself.\n\n"
            f"TASK:\n{task.prompt.strip()}"
        )
        history = task.conversation(1500)
        if history:
            user += (
                "\n\nCONVERSATION SO FAR (the task continues it — plan for what "
                f"is being asked now):\n{history}"
            )
        if state_preview:
            user += (
                "\n\nA SAMPLE of the material this task will run against "
                "(plan for the shape, not this instance):\n" + state_preview
            )
        if self.feedback.strip():
            user += ("\n\nTHE USER SAW YOUR LAST PLAN AND ASKED FOR THIS INSTEAD (follow it "
                     f"exactly):\n{self.feedback.strip()[:1200]}")
        text, call = llm.complete(
            [
                {"role": "system", "content": COMPILER_SYSTEM},
                {"role": "user", "content": user},
            ],
            purpose="compile",
            model=model,
            temperature=0.0,
            max_tokens=3200,
        )
        try:
            raw = json.loads(_first_json_object(text))
        except json.JSONDecodeError as exc:
            raise CompileError(f"planner JSON did not parse: {exc}") from exc
        plan = Plan.from_dict(raw, source=SOURCE_COMPILED)
        return self._repair(plan, task), call

    @staticmethod
    def _repair(plan: Plan, task: Task) -> Plan:
        """Fix a plan that cannot deliver what the task asked for.

        Two failures are worth catching, because both end with the user holding
        nothing:

        1. A decisions-only plan for a task that plainly asks for writing —
           "reply if warranted" becomes four judgements and no reply.
        2. A gate on a task that was never conditional. A gate answers "is any
           writing warranted here", which is a real question for "reply only if
           needed" and a nonsensical one for "explain X in three sentences". A
           planner model will still write one, and then it fires, and the answer
           is silently withheld.
        """
        gate = plan.gate
        if gate.skip_generation_when and not CONDITIONAL.search(task.prompt):
            gate = Gate()
        steps = plan.steps
        if steps and TINY.search(task.prompt):
            steps = ()
        needs_generation = plan.generation is None and bool(GENERATION_VERBS.search(task.prompt))
        if gate is plan.gate and steps is plan.steps and not needs_generation:
            return plan
        repaired = Plan(
            strategy=plan.strategy,
            answer_from=FROM_GENERATION if (needs_generation or plan.generation) else plan.answer_from,
            decisions=plan.decisions,
            gate=gate,
            generation=plan.generation or Generation(
                instruction=task.prompt.strip(), include_state=bool(task.state_text)
            ),
            guard=plan.guard,
            research=plan.research,
            steps=steps,
            source=plan.source,
            strategy_code=plan.strategy_code,
            notes=" ".join(filter(None, [
                plan.notes,
                "Generation step restored: the task asks for text." if needs_generation else "",
                "Gate dropped: the task is not conditional." if gate is not plan.gate else "",
                "Steps dropped: the task asks for something tiny." if steps is not plan.steps else "",
            ])).strip(),
        )
        repaired.validate()
        return repaired

    # -- last resort -------------------------------------------------------- #

    def _fallback(self, task: Task, why: str) -> Plan:
        plan = Plan(
            strategy="LLM only",
            answer_from=FROM_GENERATION,
            generation=Generation(
                instruction=task.prompt.strip(), include_state=bool(task.state_text)
            ),
            source=SOURCE_FALLBACK,
            strategy_code="llm_only",
            notes=f"Planning was skipped: {why}",
        )
        plan.validate()
        return plan


def _retagged(plan: Plan, source: str) -> Plan:
    return Plan(
        strategy_code=plan.strategy_code,
        strategy=plan.strategy,
        answer_from=plan.answer_from,
        decisions=plan.decisions,
        gate=plan.gate,
        generation=plan.generation,
        guard=plan.guard,
        research=plan.research,
        steps=plan.steps,
        source=source,
        notes=plan.notes,
    )


def _single_decision_plan(name: str, question, why: str) -> Plan:
    plan = Plan(
        strategy=f"one {question.kind} decision",
        answer_from=FROM_DECISIONS,
        decisions={name: question},
        source=SOURCE_DETERMINISTIC,
        strategy_code=f"one_{question.kind}",
        notes=f"Recognised locally: {why}. No planning call was made.",
    )
    plan.validate()
    return plan


def _leading_noun(prompt: str) -> str:
    match = re.search(r"\b(?:this|the)\s+([a-z]{3,20})\b", prompt, re.IGNORECASE)
    return match.group(1) if match else "category"


def _rating_levels(prompt: str) -> List[str]:
    """Build score levels from an explicit numeric range, else a default ladder."""
    match = re.search(r"\b(\d{1,2})\s*(?:-|to|~|–)\s*(\d{1,3})\b", prompt)
    if match:
        low, high = int(match.group(1)), int(match.group(2))
        if 0 <= low < high and high - low <= 20:
            return [f"{n}" for n in range(low, high + 1)]
    return [
        "Not at all — clearly at the bottom of the range",
        "Slightly",
        "Moderately",
        "Considerably",
        "Extremely — clearly at the top of the range",
    ]
