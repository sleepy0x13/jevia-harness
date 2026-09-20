"""The app factory: interfaces composed at run time, not written.

A model asked for a screen writes HTML token by token and gets something
different every time. Here the pieces already exist — a catalogue of components
with fixed shapes and a renderer that knows how to draw them — and the work
splits the way everything else in this harness does:

  1. Jev decides the structure: which components, how high on the page, and the
     overall layout. Every component is a yes/no and a position, so the whole
     composition is one call.
  2. If the layout has two columns, a second Jev call puts each component in
     one of them. Only then.
  3. A cheap model fills in the words — labels, rows, options — as a small JSON
     object. It never writes markup, so it cannot produce a broken page.
  4. The renderer turns that into one self-contained, working HTML file.

Everything a model produced is escaped on the way out.
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .policy import today
from .providers import Call, JevClient, LLMClient
from .questions import Choice, Noul, Score, parse_answer

KEEP_ABOVE = 0.6
POSITIONS = ("the very top of the page", "high on the page", "the middle",
             "low on the page", "the very bottom")
KINDS = {
    "compose": "A data screen made of standard parts: forms, tables, lists, cards, charts, "
               "trackers, dashboards, calculators or booking pages with simple logic.",
    "code": "Something with behaviour of its own that standard parts cannot express: a game, "
            "animation, drawing board, simulation, timer, visual toy or custom interactive tool.",
}
LAYOUTS = {
    "single": "One column, top to bottom — a form, a report, a single task.",
    "split": "A wide main area with a narrow side panel for filters, a summary or a secondary form.",
    "grid": "A dashboard: several tiles of equal weight shown at once.",
}


@dataclass(frozen=True)
class Component:
    id: str
    summary: str          # what Jev reads to decide whether the screen needs it
    props: str            # the shape the filler must produce, as an example


CATALOGUE: Tuple[Component, ...] = (
    Component("hero", "A page title with a one-line explanation of what the screen is for.",
              '{"title": "…", "subtitle": "…"}'),
    Component("stats", "A row of key numbers with labels, for an at-a-glance summary.",
              '{"items": [{"label": "…", "value": "…", "note": "…"}]}'),
    Component("form", "Input fields the user fills in and submits: booking, sign-up, a calculator, a request.",
              '{"title": "…", "fields": [{"name": "…", "label": "…", "type": "text|number|select|date|email|textarea|checkbox", "options": ["…"]}], "submit": "…"}'),
    Component("table", "Rows of records with columns, for comparing many items.",
              '{"title": "…", "columns": ["…"], "rows": [["…"]]}'),
    Component("cards", "A grid of cards, each an item with a title, a short description and an action.",
              '{"title": "…", "items": [{"title": "…", "body": "…", "meta": "…", "action": "…"}]}'),
    Component("chart", "A bar or line chart of values over categories or time.",
              '{"title": "…", "kind": "bar|line", "series": [{"label": "…", "value": 0}]}'),
    Component("filter", "A search box that filters the list, table or cards on the page.",
              '{"placeholder": "…"}'),
    Component("checklist", "Tickable tasks with a progress count.",
              '{"title": "…", "items": ["…"]}'),
    Component("timeline", "Events or steps in time order.",
              '{"title": "…", "items": [{"time": "…", "title": "…", "detail": "…"}]}'),
    Component("tabs", "Several sections of text, one visible at a time.",
              '{"tabs": [{"title": "…", "body": "…"}]}'),
    Component("steps", "A numbered progress indicator for a multi-stage process.",
              '{"items": ["…"], "current": 1}'),
    Component("text", "A block of explanatory prose.",
              '{"title": "…", "body": "…"}'),
    Component("list", "A plain bulleted list of points.",
              '{"title": "…", "items": ["…"]}'),
    Component("callout", "A highlighted notice: a warning, a tip or a key fact.",
              '{"tone": "info|warn|good", "body": "…"}'),
)
BY_ID: Dict[str, Component] = {c.id: c for c in CATALOGUE}


@dataclass
class Composition:
    components: List[str] = field(default_factory=list)   # in page order
    kind: str = "compose"                                 # "compose" | "code"
    layout: str = "single"
    side: List[str] = field(default_factory=list)         # components in the side panel
    probabilities: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"components": self.components, "kind": self.kind, "layout": self.layout, "side": self.side,
                "probabilities": {k: round(v, 3) for k, v in self.probabilities.items()}}


def compose(jev: JevClient, request: str, material: str = "", *,
            observer: Any = None) -> Tuple[Composition, List[Call]]:
    """Jev picks the pieces and their order in one call, and a layout."""
    calls: List[Call] = []
    questions: Dict[str, dict] = {}
    for c in CATALOGUE:
        questions[f"use_{c.id}"] = Noul(instructions=(
            f"Does this screen need a '{c.id}' component? — {c.summary}")).to_payload()
        questions[f"pos_{c.id}"] = Score(instructions=(
            f"If the screen has a '{c.id}', where should it sit?"), levels=POSITIONS).to_payload()
    questions["layout"] = Choice(instructions="Which overall layout fits this screen?",
                                 options=LAYOUTS).to_payload()
    questions["kind"] = Choice(instructions="Can this be built from standard parts, or does it "
                                            "need code of its own?", options=KINDS).to_payload()
    state = f"THE SCREEN THE USER WANTS\n{request.strip()}"
    if material.strip():
        state += f"\n\nDATA IT SHOULD SHOW\n{material.strip()[:6000]}"
    raw, call = jev.decide(state, questions, purpose="compose", observer=observer,
                           targets={q: "the screen" for q in questions})
    calls.append(call)
    kind = "compose"
    if raw.get("kind"):
        kind = str(getattr(parse_answer("kind", raw["kind"]), "value", "compose"))
    if kind == "code":
        # Nothing to lay out: the page will be written, not assembled.
        return Composition(components=[], kind="code"), calls

    chosen: List[Tuple[float, str]] = []
    probabilities: Dict[str, float] = {}
    for c in CATALOGUE:
        answer = raw.get(f"use_{c.id}")
        p = float(getattr(parse_answer(f"use_{c.id}", answer), "probability", 0.0)) if answer else 0.0
        probabilities[c.id] = p
        if p >= KEEP_ABOVE:
            pos = raw.get(f"pos_{c.id}")
            where = float(getattr(parse_answer(f"pos_{c.id}", pos), "value", 2.0)) if pos else 2.0
            chosen.append((where, c.id))
    # A page with nothing on it is not an answer; fall back to its two
    # likeliest pieces rather than an empty screen.
    if not chosen:
        best = sorted(probabilities.items(), key=lambda kv: -kv[1])[:2]
        chosen = [(i, cid) for i, (cid, _) in enumerate(best)]
    chosen.sort()
    components = [cid for _, cid in chosen]
    if "hero" in components:                      # a title belongs at the top
        components.remove("hero")
        components.insert(0, "hero")

    layout = "single"
    answer = raw.get("layout")
    if answer:
        layout = str(getattr(parse_answer("layout", answer), "value", "single"))
    if layout not in LAYOUTS:
        layout = "single"

    side: List[str] = []
    placeable = [c for c in components if c != "hero"]
    if layout == "split" and len(placeable) > 1:
        # Only now is a second question worth asking.
        layout_q = {f"side_{c}": Noul(instructions=(
            f"Should the '{c}' go in the narrow side panel rather than the main area?")).to_payload()
            for c in placeable}
        raw2, call2 = jev.decide(state + "\n\nCOMPONENTS\n" + ", ".join(placeable), layout_q,
                                 purpose="layout", observer=observer,
                                 targets={f"side_{c}": c for c in placeable})
        calls.append(call2)
        for c in placeable:
            ans = raw2.get(f"side_{c}")
            if ans and float(getattr(parse_answer(f"side_{c}", ans), "probability", 0.0)) >= 0.5:
                side.append(c)
        if len(side) == len(placeable):         # everything aside is no layout at all
            side = side[-1:]
    elif layout == "split":
        layout = "single"
    return Composition(components=components, layout=layout, side=side, probabilities=probabilities), calls


FILL_SYSTEM = """\
You fill in the words for a user interface that has already been designed. \
Return JSON only — no prose, no code fences, no markup of any kind.

Return {"title": "page title", "components": [ ... ]} with exactly one entry \
per component listed, in the same order, each shaped {"type": "<id>", "props": \
{...}} using the shape shown for it. Use realistic, specific content drawn from \
the request and any data given; invent plausible sample values only where none \
were given. Keep text short: labels are words, not sentences. Write every \
visible string in the language of the request."""


def fill(llm: LLMClient, request: str, composition: Composition, material: str = "",
         model: Optional[str] = None, conversation: str = "") -> Tuple[dict, Call]:
    """A cheap model writes the words, and only the words."""
    if conversation:
        request = f"{request.strip()}\n\nEARLIER IN THIS CONVERSATION\n{conversation}"
    listing = "\n".join(f'{i + 1}. {cid}: {BY_ID[cid].props}' for i, cid in enumerate(composition.components))
    user = f"TODAY\n{today()}\n\nREQUEST\n{request.strip()}\n\nCOMPONENTS, IN ORDER\n{listing}"
    if material.strip():
        user += f"\n\nDATA\n{material.strip()[:8000]}"
    text, call = llm.complete([{"role": "system", "content": FILL_SYSTEM},
                               {"role": "user", "content": user}],
                              purpose="fill", model=model, temperature=0.2, max_tokens=2600)
    return _parse_spec(text, composition), call


def _parse_spec(text: str, composition: Composition) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    spec: Dict[str, Any] = {}
    if start >= 0 and end > start:
        try:
            spec = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            spec = {}
    given = [c for c in (spec.get("components") or []) if isinstance(c, Mapping)]
    by_type: Dict[str, List[Mapping]] = {}
    for c in given:
        by_type.setdefault(str(c.get("type")), []).append(c)
    ordered, dropped = [], []
    for cid in composition.components:
        pool = by_type.get(cid) or []
        props = pool.pop(0).get("props") if pool else {}
        if not props or not isinstance(props, Mapping):
            # An empty block is noise, not a component — but the drop is
            # reported, so a schema failure is never shown as a finished page.
            dropped.append(cid)
            continue
        ordered.append({"type": cid, "props": props})
    return {"title": str(spec.get("title") or "").strip() or "App", "components": ordered,
            "dropped": dropped, "parsed": bool(spec)}


# --------------------------------------------------------------------------- #
# The renderer
# --------------------------------------------------------------------------- #

def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _opt(tag: str, value: Any, attrs: str = "") -> str:
    """An element only when there is something to put in it."""
    if value in (None, ""):
        return ""
    return f"<{tag}{' ' + attrs if attrs else ''}>{_e(value)}</{tag}>"


def _items(props: Mapping, key: str = "items") -> list:
    items = props.get(key)
    return items if isinstance(items, list) else []


def _r_hero(p: Mapping) -> str:
    return f'<header class="hero">{STAR}<h1>{_e(p.get("title"))}</h1>{_opt("p", p.get("subtitle"))}</header>'


def _r_stats(p: Mapping) -> str:
    cells = "".join(
        f'<div class="stat"><div class="k">{_e(i.get("label"))}</div><div class="v">{_e(i.get("value"))}</div>'
        f'{_opt("div", i.get("note"), "class=n")}</div>'
        for i in _items(p) if isinstance(i, Mapping))
    return f'<section class="stats">{cells}</section>'


def _field(f: Mapping) -> str:
    name = re.sub(r"[^a-zA-Z0-9_]", "_", str(f.get("name") or f.get("label") or "field"))[:40]
    label = _e(f.get("label") or name)
    kind = str(f.get("type") or "text")
    if kind == "select":
        opts = "".join(f"<option>{_e(o)}</option>" for o in (f.get("options") or []))
        control = f'<select name="{_e(name)}">{opts}</select>'
    elif kind == "textarea":
        control = f'<textarea name="{_e(name)}" rows="3"></textarea>'
    elif kind == "checkbox":
        return f'<label class="cb"><input type="checkbox" name="{_e(name)}"> {label}</label>'
    else:
        kind = kind if kind in ("text", "number", "date", "email", "tel", "time") else "text"
        control = f'<input type="{kind}" name="{_e(name)}">'
    return f'<label class="f"><span>{label}</span>{control}</label>'


def _r_form(p: Mapping) -> str:
    fields = "".join(_field(f) for f in _items(p, "fields") if isinstance(f, Mapping))
    return (f'<section class="card"><h2>{_e(p.get("title"))}</h2><form data-form>{fields}'
            f'<button type="submit">{_e(p.get("submit") or "Submit")}</button></form>'
            f'<div class="out" hidden></div></section>')


def _r_table(p: Mapping) -> str:
    cols = [c for c in (p.get("columns") or [])]
    head = "".join(f"<th>{_e(c)}</th>" for c in cols)
    rows = "".join("<tr data-row>" + "".join(f"<td>{_e(v)}</td>" for v in (r if isinstance(r, list) else [r])) + "</tr>"
                   for r in (p.get("rows") or []))
    return (f'<section class="card"><h2>{_e(p.get("title"))}</h2><div class="scroll"><table>'
            f'<thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div></section>')


def _r_cards(p: Mapping) -> str:
    cards = "".join(
        f'<article class="item" data-row><h3>{_e(i.get("title"))}</h3><p>{_e(i.get("body"))}</p>'
        f'<div class="meta"><span>{_e(i.get("meta"))}</span>'
        f'{_opt("button", i.get("action"), "data-pick")}</div></article>'
        for i in _items(p) if isinstance(i, Mapping))
    return f'<section><h2>{_e(p.get("title"))}</h2><div class="cards">{cards}</div></section>'


def _r_chart(p: Mapping) -> str:
    series = [s for s in _items(p, "series") if isinstance(s, Mapping)]
    values = []
    for s in series:
        try:
            values.append(float(s.get("value") or 0))
        except (TypeError, ValueError):
            values.append(0.0)
    if not values:
        return ""
    top = max(max(values), 1e-9)
    w, h, pad = 560, 220, 28
    step = (w - pad * 2) / max(len(values), 1)
    kind = str(p.get("kind") or "bar")
    parts = []
    if kind == "line" and len(values) > 1:
        pts = " ".join(f"{pad + step * i + step / 2:.1f},{h - pad - (v / top) * (h - pad * 2):.1f}"
                       for i, v in enumerate(values))
        parts.append(f'<polyline points="{pts}" fill="none" stroke="var(--blue)" stroke-width="2.5"/>')
        for i, v in enumerate(values):
            x = pad + step * i + step / 2
            y = h - pad - (v / top) * (h - pad * 2)
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="var(--ink)"/>')
    else:
        bw = step * 0.62
        for i, v in enumerate(values):
            bh = (v / top) * (h - pad * 2)
            x = pad + step * i + (step - bw) / 2
            parts.append(f'<rect x="{x:.1f}" y="{h - pad - bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" fill="var(--blue)"/>')
    for i, s in enumerate(series):
        x = pad + step * i + step / 2
        parts.append(f'<text x="{x:.1f}" y="{h - 8}" text-anchor="middle">{_e(s.get("label"))}</text>')
        parts.append(f'<text x="{x:.1f}" y="{h - pad - (values[i] / top) * (h - pad * 2) - 7:.1f}" '
                     f'text-anchor="middle" class="val">{_e(s.get("value"))}</text>')
    return (f'<section class="card"><h2>{_e(p.get("title"))}</h2>'
            f'<svg viewBox="0 0 {w} {h}" class="chart">{"".join(parts)}'
            f'<line x1="{pad}" y1="{h - pad}" x2="{w - pad}" y2="{h - pad}" stroke="var(--ink)"/></svg></section>')


def _r_filter(p: Mapping) -> str:
    return f'<input class="filter" data-filter placeholder="{_e(p.get("placeholder") or "Search…")}">'


def _r_checklist(p: Mapping) -> str:
    items = "".join(f'<label class="cb"><input type="checkbox" data-check> {_e(i)}</label>' for i in _items(p))
    return (f'<section class="card"><h2>{_e(p.get("title"))} <small data-progress></small></h2>'
            f'<div class="checks">{items}</div></section>')


def _r_timeline(p: Mapping) -> str:
    rows = "".join(f'<li><time>{_e(i.get("time"))}</time><b>{_e(i.get("title"))}</b><p>{_e(i.get("detail"))}</p></li>'
                   for i in _items(p) if isinstance(i, Mapping))
    return f'<section class="card"><h2>{_e(p.get("title"))}</h2><ol class="timeline">{rows}</ol></section>'


def _r_tabs(p: Mapping) -> str:
    tabs = [t for t in _items(p, "tabs") if isinstance(t, Mapping)]
    heads = "".join(f'<button data-tab="{i}" class="{"on" if i == 0 else ""}">{_e(t.get("title"))}</button>'
                    for i, t in enumerate(tabs))
    bodies = "".join(f'<div data-pane="{i}" {"" if i == 0 else "hidden"}>{_e(t.get("body"))}</div>'
                     for i, t in enumerate(tabs))
    return f'<section class="card tabs"><nav>{heads}</nav>{bodies}</section>'


def _r_steps(p: Mapping) -> str:
    try:
        current = int(p.get("current") or 1)
    except (TypeError, ValueError):
        current = 1
    items = "".join(f'<li class="{"done" if i + 1 < current else "on" if i + 1 == current else ""}">'
                    f'<i>{i + 1}</i>{_e(s)}</li>' for i, s in enumerate(_items(p)))
    return f'<ol class="steps">{items}</ol>'


def _r_text(p: Mapping) -> str:
    return f'<section class="card"><h2>{_e(p.get("title"))}</h2><p>{_e(p.get("body"))}</p></section>'


def _r_list(p: Mapping) -> str:
    items = "".join(f"<li data-row>{_e(i)}</li>" for i in _items(p))
    return f'<section class="card"><h2>{_e(p.get("title"))}</h2><ul>{items}</ul></section>'


def _r_callout(p: Mapping) -> str:
    tone = str(p.get("tone") or "info")
    tone = tone if tone in ("info", "warn", "good") else "info"
    return f'<aside class="callout {tone}">{_e(p.get("body"))}</aside>'


RENDERERS = {
    "hero": _r_hero, "stats": _r_stats, "form": _r_form, "table": _r_table, "cards": _r_cards,
    "chart": _r_chart, "filter": _r_filter, "checklist": _r_checklist, "timeline": _r_timeline,
    "tabs": _r_tabs, "steps": _r_steps, "text": _r_text, "list": _r_list, "callout": _r_callout,
}

# Root-relative, so the preview served by JEVia gets the house typeface and a
# copy opened straight from disk falls back to the system one.
FONTS = ("@font-face{font-family:Space;src:url('/ui/fonts/Space-400.ttf');font-weight:300 500}"
         "@font-face{font-family:Space;src:url('/ui/fonts/Space-600-800.ttf');font-weight:600 800}")

# The generated app wears the same clothes as JEVia: paper white, one blue,
# light grey panels, hairlines, calm large type, and a single asterisk.
STAR = ('<svg class="star" viewBox="0 0 100 100" aria-hidden="true"><path d="M50 3v94M3 50h94'
        'M16.8 16.8l66.4 66.4M83.2 16.8L16.8 83.2" fill="none" stroke="currentColor" stroke-width="3"/></svg>')

APP_CSS = """
:root{--paper:#fcfdfd;--grey:#f1f3f5;--white:#fff;--ink:#19232d;--soft:#667583;--mute:#71808b;--line:#e3e8ec;--line-2:#cfd7dd;
  --blue:#3158ef;--wash:#eef2fe;--red:#c8412c;--ease:cubic-bezier(.22,1,.36,1)}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font:400 15px/1.65 Space,-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;padding:40px 44px;-webkit-font-smoothing:antialiased}
.page{max-width:1080px;margin:0 auto;display:grid;gap:34px}
.page.split{grid-template-columns:minmax(0,1fr) 320px;align-items:start}
.page.split .hero{grid-column:1/-1}
.page.grid{grid-template-columns:repeat(auto-fill,minmax(320px,1fr))}
.page.grid .hero,.page.grid .stats,.page.grid .steps{grid-column:1/-1}
.col{display:grid;gap:34px;min-width:0}
h1{font-size:46px;font-weight:500;letter-spacing:-.03em;line-height:1.1;margin:0}
h2{font-size:13px;font-weight:500;color:var(--blue);letter-spacing:.02em;margin:0 0 14px}
h2 small{color:var(--mute);font-weight:400;margin-left:8px}
h3{font-size:16px;font-weight:500;margin:0 0 6px}
.hero{position:relative;padding-right:90px}
.hero p{margin:14px 0 0;font-size:16px;color:var(--soft);max-width:620px}
.hero .star{position:absolute;right:0;top:4px;width:56px;height:56px;color:var(--ink)}
.card{border-top:1px solid var(--ink);padding-top:16px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));border-top:1px solid var(--ink)}
.stat{padding:16px 18px 4px 0}
.stat+.stat{padding-left:18px;border-left:1px solid var(--line)}
.stat .k{font-size:12.5px;color:var(--mute)}
.stat .v{font-size:40px;font-weight:400;letter-spacing:-.04em;line-height:1.1;margin-top:6px}
.stat .n{font-size:12.5px;color:var(--blue);margin-top:2px}
form{display:grid;gap:14px}
.f span{display:block;font-size:12.5px;color:var(--mute);margin-bottom:6px}
input,select,textarea{width:100%;font:inherit;padding:10px 12px;border:1px solid var(--line-2);border-radius:3px;background:var(--white);color:var(--ink);transition:border-color .2s,box-shadow .2s}
input:focus,select:focus,textarea:focus{outline:0;border-color:var(--blue);box-shadow:0 0 0 3px #3158ef1f}
.cb{display:flex;gap:10px;align-items:center;font-size:14.5px;padding:6px 0}
.cb input{width:auto;accent-color:var(--blue)}
button{font:inherit;font-weight:500;border:0;border-radius:3px;background:var(--blue);color:#fff;padding:11px 18px;cursor:pointer;transition:background .2s,transform .45s var(--ease)}
button:hover{background:var(--ink)}
button:active{transform:translateY(1px)}
.out{margin-top:16px;padding:14px 16px;background:var(--grey);border-radius:3px;font-size:14px;animation:lift .6s var(--ease)}
.out dl{display:grid;grid-template-columns:auto 1fr;gap:6px 16px;margin:0}
.out dt{color:var(--mute)}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:14px}
th{white-space:nowrap;text-align:left;font-size:12.5px;font-weight:400;color:var(--mute);border-bottom:1px solid var(--ink);padding:8px 12px 8px 0}
td{border-bottom:1px solid var(--line);padding:11px 12px 11px 0}
tr:hover td{color:var(--blue)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:8px}
.item{background:var(--grey);border-radius:3px;padding:18px;display:flex;flex-direction:column;transition:background .2s,color .2s}
.item:hover{background:var(--wash)}
.item p{font-size:14px;color:var(--soft);margin:0 0 16px;flex:1}
.item .meta{display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:12.5px;color:var(--mute)}
.item.picked{background:var(--blue);color:#fff}
.item.picked p,.item.picked .meta{color:#ffffffcc}
.item.picked button{background:#fff;color:var(--blue)}
.item button{padding:7px 12px;font-size:13px}
.chart{width:100%;max-width:640px;height:auto;font-size:11px;fill:var(--mute)}
.chart .val{fill:var(--ink)}
.filter{font-size:16px;padding:14px 16px;border-color:var(--ink)}
.timeline{list-style:none;margin:0;padding:0}
.timeline li{display:grid;grid-template-columns:100px 1fr;gap:4px 16px;padding:12px 0;border-bottom:1px solid var(--line)}
.timeline time{font-size:13px;color:var(--blue);grid-row:span 2}
.timeline b{font-weight:500}
.timeline p{margin:0;font-size:13.5px;color:var(--mute)}
.tabs nav{display:flex;gap:22px;border-bottom:1px solid var(--line);margin-bottom:16px}
.tabs nav button{background:transparent;color:var(--mute);padding:10px 0;border-radius:0;position:relative}
.tabs nav button::after{content:"";position:absolute;left:0;right:0;bottom:-1px;height:1.5px;background:var(--blue);transform:scaleX(0);transform-origin:0 50%;transition:transform .45s var(--ease)}
.tabs nav button:hover{background:transparent;color:var(--ink)}
.tabs nav button.on{color:var(--ink)}
.tabs nav button.on::after{transform:scaleX(1)}
.steps{list-style:none;margin:0;padding:0;display:flex;border-top:1px solid var(--ink)}
.steps li{flex:1;padding:14px 14px 4px 0;font-size:14px;color:var(--mute)}
.steps li i{font-style:normal;display:block;font-size:12.5px;margin-bottom:4px}
.steps li.on{color:var(--ink)}
.steps li.on i{color:var(--blue)}
.steps li.done{color:var(--ink)}
ul{margin:0;padding-left:20px}
li::marker{color:var(--blue)}
.callout{padding:16px 18px;border-left:2px solid var(--blue);background:var(--wash);font-size:14.5px}
.callout.warn{border-color:var(--red);background:#c8412c0d}
.callout.good{border-color:var(--ink);background:var(--grey)}
[hidden]{display:none!important}
@keyframes curtain{from{clip-path:inset(0 100% 0 0);transform:translate3d(-18px,0,0)}to{clip-path:inset(0);transform:none}}
@keyframes lift{from{opacity:0;transform:translate3d(0,20px,0)}to{opacity:1;transform:none}}
@keyframes type{from{clip-path:inset(100% 0 0 0);transform:translate3d(0,26px,0);opacity:.2}to{clip-path:inset(0);transform:none;opacity:1}}
@keyframes starin{from{transform:rotate(-90deg) scale(.64);opacity:0}to{transform:none;opacity:1}}
.page>*,.page>.col>*{animation:curtain .66s var(--ease) both;animation-delay:calc(var(--i,0) * 110ms)}
.hero h1{animation:type .9s var(--ease) both}
.hero .star{animation:starin 1.05s var(--ease) .2s both}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
@media (max-width:760px){.page.split{grid-template-columns:1fr}body{padding:22px 18px}h1{font-size:32px}.hero{padding-right:0}.hero .star{display:none}.steps{flex-wrap:wrap}}
"""

APP_JS = """
(function(){
  var rows=[].slice.call(document.querySelectorAll('[data-row]'));
  function text(r){return r.textContent.toLowerCase();}
  document.querySelectorAll('[data-form]').forEach(function(form){
    form.addEventListener('submit',function(e){
      e.preventDefault();
      var out=form.parentElement.querySelector('.out'), shown=[], terms=[], total=0, nums=0;
      [].forEach.call(form.elements,function(el){
        if(!el.name)return;
        var v=el.type==='checkbox'?(el.checked?'✓':''):String(el.value||'').trim();
        var box=el.closest('label'), cap=box&&box.querySelector('span');
        var label=(cap?cap.textContent:box?box.textContent:el.name).trim();
        shown.push('<dt>'+esc(label)+'</dt><dd>'+esc(v||'—')+'</dd>');
        if(el.type==='number'&&v!==''){total+=parseFloat(v)||0;nums++;}
        // A value that matches no row ("All", a date the table does not list)
        // says nothing about which rows to keep, so it is not a filter.
        var q=v.toLowerCase();
        if(q&&el.type!=='checkbox'&&rows.some(function(r){return text(r).indexOf(q)>=0;}))terms.push(q);
      });
      if(nums>1)shown.push('<dt>Σ</dt><dd>'+total+'</dd>');
      var kept=rows.length;
      if(rows.length){
        kept=0;
        rows.forEach(function(r){
          var ok=terms.every(function(q){return text(r).indexOf(q)>=0;});
          r.hidden=!ok; if(ok)kept++;
        });
        shown.push('<dt>→</dt><dd>'+kept+' / '+rows.length+'</dd>');
      }
      out.innerHTML='<dl>'+shown.join('')+'</dl>'; out.hidden=false;
    });
  });
  document.querySelectorAll('[data-filter]').forEach(function(box){
    box.addEventListener('input',function(){
      var q=box.value.trim().toLowerCase();
      rows.forEach(function(r){r.hidden=q&&text(r).indexOf(q)<0;});
    });
  });
  document.querySelectorAll('.checks').forEach(function(list){
    var tag=list.parentElement.querySelector('[data-progress]');
    function count(){var all=list.querySelectorAll('[data-check]'),done=list.querySelectorAll('[data-check]:checked');
      if(tag)tag.textContent=done.length+'/'+all.length;}
    list.addEventListener('change',count); count();
  });
  document.querySelectorAll('.tabs').forEach(function(t){
    t.querySelectorAll('[data-tab]').forEach(function(b){
      b.addEventListener('click',function(){
        t.querySelectorAll('[data-tab]').forEach(function(x){x.classList.toggle('on',x===b);});
        t.querySelectorAll('[data-pane]').forEach(function(p){p.hidden=p.dataset.pane!==b.dataset.tab;});
      });
    });
  });
  document.querySelectorAll('[data-pick]').forEach(function(b){
    b.addEventListener('click',function(){b.closest('.item').classList.toggle('picked');});
  });
  function esc(s){return String(s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
})();
"""


def render(spec: Mapping, composition: Composition) -> str:
    """One self-contained page. No model-written markup survives into it."""
    blocks: Dict[str, str] = {}
    for item in spec.get("components") or []:
        cid = str(item.get("type"))
        renderer = RENDERERS.get(cid)
        if renderer is None:
            continue
        try:
            blocks.setdefault(cid, renderer(item.get("props") or {}))
        except Exception:  # noqa: BLE001 - one bad block must not sink the page
            continue
    order = [c for c in composition.components if c in blocks]
    # Each block learns its place in the order, so the page assembles itself.
    for n, cid in enumerate(order):
        blocks[cid] = re.sub(r"^<(\w+)", lambda m: f'<{m.group(1)} style="--i:{n}"', blocks[cid], count=1)
    if composition.layout == "split" and composition.side:
        top = "".join(blocks[c] for c in order if c == "hero")
        main = "".join(blocks[c] for c in order if c != "hero" and c not in composition.side)
        side = "".join(blocks[c] for c in order if c in composition.side)
        body = f'{top}<div class="col">{main}</div><div class="col">{side}</div>'
    else:
        body = "".join(blocks[c] for c in order)
    return ("<!doctype html><html><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{_e(spec.get('title'))}</title><style>{FONTS}{APP_CSS}</style></head>"
            f"<body><main class=\"page {composition.layout}\">{body}</main>"
            f"<script>{APP_JS}</script></body></html>")


def summary(spec: Mapping, composition: Composition) -> str:
    """The line that stands in the chat for the app itself."""
    return f"**{spec.get('title') or 'App'}** · app.html"


# --------------------------------------------------------------------------- #
# Apps that need code of their own
# --------------------------------------------------------------------------- #

WRITE_SYSTEM = """\
You build one complete, working web app as a single HTML file. It runs in a \
sandboxed frame with no network: every line of CSS and JavaScript is inline, and \
nothing is loaded from anywhere — no CDNs, fonts, images or requests (draw with \
CSS, SVG or canvas). It must work at once with mouse, touch and keyboard, fill \
the frame, and adapt from phone to desktop width. Handle start, restart and \
game-over or empty states. Visual style — calm editorial: paper white #fcfdfd, \
ink #19232d, one blue #3158ef used as solid fields and accents, light grey \
#f1f3f5 panels, hairline rules #e3e8ec; 3px corners at most; no drop shadows, \
gradients, glows or neon; large, quiet type (system-ui, headings weight 500, \
tight letter-spacing) with generous whitespace; a thin eight-line asterisk as \
the only ornament. Motion is brief and purposeful — reveal with a horizontal \
wipe or a short rise over about 0.7s on cubic-bezier(.22,1,.36,1); nothing \
flashes or loops except the app's own logic. All visible text in the language \
of the request. Return only the document, starting with <!doctype html> — no \
explanation and no code fences."""

PAGE_RE = re.compile(r"```(?:html|htm)?\s*\n(?P<page>.*?)```", re.S | re.I)


def extract_page(text: str) -> str:
    """A whole HTML document out of a model's reply, or "" when there is none.

    Fenced or bare, it counts only if it is a page and not a snippet: it has
    to open a document or a body and carry some behaviour or layout of its own.
    """
    text = text or ""
    candidates = [m.group("page") for m in PAGE_RE.finditer(text)]
    lowered = text.lower()
    start = lowered.find("<!doctype")
    if start < 0:
        start = lowered.find("<html")
    if start >= 0:
        end = lowered.rfind("</html>")
        candidates.append(text[start:end + 7] if end > start else text[start:])
    for page in sorted(candidates, key=len, reverse=True):
        low = page.lower()
        if ("<html" in low or "<body" in low) and ("<script" in low or "<style" in low) and len(page) > 300:
            return page.strip()
    return ""


def strip_page(text: str, page: str) -> str:
    """The reply with the page taken out, so the reader never sees the code."""
    if not page:
        return text
    out = text.replace(f"```html\n{page}\n```", "").replace(f"```\n{page}\n```", "").replace(page, "")
    out = PAGE_RE.sub("", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def page_title(page: str, fallback: str = "App") -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", page or "", re.S | re.I)
    title = html.unescape(match.group(1)).strip() if match else ""
    return title[:80] or fallback


def write_messages(request: str, material: str = "", conversation: str = "") -> List[dict]:
    user = f"TODAY\n{today()}\n\nREQUEST\n{request.strip()}"
    if conversation:
        user += f"\n\nEARLIER IN THIS CONVERSATION (an earlier version may be here — improve on it)\n{conversation[-6000:]}"
    if material.strip():
        user += f"\n\nMATERIAL\n{material.strip()[:8000]}"
    return [{"role": "system", "content": WRITE_SYSTEM}, {"role": "user", "content": user}]


# What a page being written is made of, read off the code as it streams in, so
# the reader watches real parts arrive: the stylesheet, the canvas, the loop,
# each named function. Only names — never the code itself.
MODULE_PATTERNS = (
    (re.compile(r"<style", re.I), "style"),
    (re.compile(r"<canvas", re.I), "canvas"),
    (re.compile(r"<(header|nav|main|section|aside|footer|form|table|svg)\b", re.I), None),
    (re.compile(r"\bclass\s+([A-Z][A-Za-z0-9_]{2,30})"), None),
    (re.compile(r"\bfunction\s+([A-Za-z_][A-Za-z0-9_]{2,30})\s*\("), None),
    (re.compile(r"\b(?:const|let)\s+([A-Za-z_][A-Za-z0-9_]{2,30})\s*=\s*(?:async\s*)?(?:function|\([^)]*\)\s*=>)"), None),
    (re.compile(r"requestAnimationFrame", re.I), "loop"),
    (re.compile(r"addEventListener\(\s*['\"](key|touch|pointer|mouse)", re.I), "input"),
)
MAX_MODULES = 14


def find_modules(code: str, known: Sequence[str] = ()) -> List[str]:
    """New part names in ``code`` since ``known``, in the order they appear."""
    found: List[Tuple[int, str]] = []
    for pattern, fixed in MODULE_PATTERNS:
        for match in pattern.finditer(code):
            name = fixed or (match.group(1) if match.groups() else match.group(0))
            found.append((match.start(), name.lower() if fixed is None and pattern.pattern.startswith("<") else name))
    out: List[str] = []
    seen = set(known)
    for _, name in sorted(found):
        if name not in seen and len(seen) < MAX_MODULES:
            seen.add(name)
            out.append(name)
    return out
