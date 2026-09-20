"""Skills: markdown instruction packs that say how to do a kind of work well.

This is the same idea every serious harness ships — a folder of markdown files,
each describing one kind of task and how to do it properly — and it is where
out-of-the-box quality actually comes from. A model told "write a briefing"
produces something generic; the same model handed three paragraphs on what a
good briefing contains produces something usable.

A skill is a file:

    ---
    name: Code review
    description: Reviewing a diff or a proposed change.
    when: A diff, a pull request, or a description of a change to judge.
    ---

    ...the instructions...

Built-in skills live in ``skills/`` next to the package. A user drops their own
``.md`` files into ``<workspace>/skills/`` and they appear alongside, which is
the whole extension mechanism — no code, no restart.

Which skill applies is a typed choice with a calibrated probability, so Jev
makes it, inside a call the run was making anyway.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .questions import Choice

BUILTIN_DIR = Path(__file__).resolve().parent.parent / "skills"
NO_SKILL = "none"
MAX_BODY = 6000


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    description: str
    when: str
    body: str
    source: str = "builtin"
    path: Optional[Path] = None

    def criterion(self) -> str:
        return self.when or self.description

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "description": self.description,
                # What a list or a menu shows: long descriptions are common in
                # packs written elsewhere, and Jev still sees the whole thing.
                "summary": summarise(self.description),
                "when": self.when, "source": self.source, "chars": len(self.body),
                "editable": self.source == "workspace"}

    def raw(self) -> str:
        """The whole file, frontmatter included, exactly as a user would edit it."""
        if self.path and self.path.is_file():
            try:
                return self.path.read_text(encoding="utf-8")
            except OSError:
                pass
        return render(self.name, self.description, self.when, self.body)


FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


def render(name: str, description: str, when: str, body: str) -> str:
    lines = ["---", f"name: {name}", f"description: {description}"]
    if when and when != description:
        lines.append(f"when: {when}")
    lines += ["---", "", body.strip(), ""]
    return "\n".join(lines)


def slug(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return cleaned[:48].strip("-") or "skill"


def parse(text: str, skill_id: str, source: str = "builtin",
          path: Optional[Path] = None) -> Optional[Skill]:
    match = FRONT.match(text)
    meta: Dict[str, str] = {}
    body = text
    if match:
        body = text[match.end():]
        meta = _front_matter(match.group(1))
    description = (meta.get("description") or "").strip()
    if not description:
        return None
    name = (meta.get("name") or "").strip()
    # Packs written for other harnesses name the skill with its own id. A
    # slug is not a title, so it is written out as one for display.
    if not name or (name == skill_id and "-" in name) or (" " not in name and name.islower() and "-" in name):
        name = _titlecase(name or skill_id)
    return Skill(
        id=skill_id,
        name=name,
        description=description,
        when=(meta.get("when") or "").strip() or description,
        body=body.strip()[:MAX_BODY],
        source=source,
        path=path,
    )


def _front_matter(block: str) -> Dict[str, str]:
    """The header, including YAML block values (``description: |``).

    Skills written for other harnesses often carry several paragraphs under
    one key; reading only the first line left them with no description at all,
    so they showed up nameless in the library and in the picker.
    """
    meta: Dict[str, str] = {}
    key: Optional[str] = None
    lines: List[str] = []

    def close() -> None:
        if key:
            meta[key] = " ".join(x.strip() for x in lines if x.strip()).strip()

    for raw in block.splitlines():
        head = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", raw)
        if head and not raw.startswith((" ", "\t")):
            close()
            key, value = head.group(1).strip().lower(), head.group(2).strip()
            lines = []
            if value in ("|", ">", "|-", ">-", "|+", ">+", ""):
                continue                      # a block value: the lines follow
            meta[key] = value.strip('"').strip("'")
            key = None
        elif key:
            lines.append(raw)
    close()
    return meta


def _titlecase(text: str) -> str:
    words = re.split(r"[-_\s]+", (text or "").strip())
    return " ".join(w[:1].upper() + w[1:] for w in words if w) or text


SUMMARY_CHARS = 150


def summarise(description: str) -> str:
    """One line for a list or a menu; the whole thing still goes to Jev."""
    text = " ".join((description or "").split())
    if len(text) <= SUMMARY_CHARS:
        return text
    cut = text[:SUMMARY_CHARS]
    for stop in ("。", ". ", "；", "; ", "，", ", "):
        at = cut.rfind(stop)
        if at > SUMMARY_CHARS * 0.5:
            return cut[:at + (1 if stop in ("。", "；", "，") else 0)].strip()
    return cut.rstrip() + "…"


def _load_dir(directory: Path, source: str) -> List[Skill]:
    """Every skill in a folder: loose ``name.md`` files and ``name/SKILL.md`` folders.

    The second shape is how other harnesses package skills, so one copied in
    from elsewhere works without being renamed.
    """
    out: List[Skill] = []
    if not directory.is_dir():
        return out
    candidates = []
    try:
        candidates += [(p, p.stem) for p in sorted(directory.glob("*.md"))]
        candidates += [(p, p.parent.name) for p in sorted(directory.glob("*/SKILL.md"))]
    except OSError:
        return out
    for path, skill_id in candidates:
        try:
            skill = parse(path.read_text(encoding="utf-8"), slug(skill_id), source, path)
        except OSError:
            continue
        if skill is not None:
            out.append(skill)
    return out


class Library:
    """Built-in skills, plus whatever the workspace supplies."""

    def __init__(self, workspace: Optional[Path] = None) -> None:
        self.workspace = Path(workspace) if workspace else None
        self.skills: Dict[str, Skill] = {}
        for skill in _load_dir(BUILTIN_DIR, "builtin"):
            self.skills[skill.id] = skill
        if workspace:
            # A user's own file wins over a built-in of the same name, which is
            # how you override one without editing the package.
            for skill in _load_dir(Path(workspace) / "skills", "workspace"):
                self.skills[skill.id] = skill

    def __len__(self) -> int:
        return len(self.skills)

    def all(self) -> List[Skill]:
        return sorted(self.skills.values(), key=lambda s: s.name.lower())

    def get(self, skill_id: Optional[str]) -> Optional[Skill]:
        if not skill_id or skill_id == NO_SKILL:
            return None
        return self.skills.get(skill_id)

    def question(self, task: str) -> Optional[Choice]:
        """The choice Jev answers to pick a skill, or None when there is none."""
        available = self.all()
        if not available:
            return None
        options = {NO_SKILL: "No special guidance applies; this is ordinary work."}
        for skill in available:
            options[skill.id] = skill.criterion()[:300]
        return Choice(
            instructions=(
                "Which set of instructions best fits this task? Pick one only if "
                f"it clearly applies. The task is: {task.strip()[:400]}"
            ),
            options=options,
        )

    def catalogue(self) -> List[dict]:
        return [s.to_dict() for s in self.all()]

    # -- managing the user's own ------------------------------------------- #

    @property
    def folder(self) -> Optional[Path]:
        return (self.workspace / "skills") if self.workspace else None

    def save(self, text: str, skill_id: Optional[str] = None) -> Skill:
        """Write a skill into the workspace, where it overrides any built-in.

        Editing a built-in is saving a copy under the same id: the original is
        untouched, the copy wins, and deleting the copy brings the original back.
        """
        if self.folder is None:
            raise SkillError("skills are stored in the workspace — choose one first")
        text = (text or "").strip()
        if len(text) > MAX_FILE:
            raise SkillError(f"a skill must be under {MAX_FILE // 1000} KB")
        probe = parse(text, "probe")
        if probe is None:
            raise SkillError(
                "a skill starts with a header that has at least a description:\n"
                "---\nname: …\ndescription: …\n---"
            )
        skill_id = slug(skill_id or probe.name)
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"{skill_id}.md"
        path.write_text(text + "\n", encoding="utf-8")
        skill = parse(text, skill_id, "workspace", path)
        self.skills[skill_id] = skill
        return skill

    def delete(self, skill_id: str) -> bool:
        """Remove a workspace skill. A built-in it was shadowing comes back."""
        skill = self.skills.get(skill_id)
        if skill is None or skill.source != "workspace" or skill.path is None:
            return False
        try:
            skill.path.unlink()
        except OSError:
            return False
        del self.skills[skill_id]
        for original in _load_dir(BUILTIN_DIR, "builtin"):
            if original.id == skill_id:
                self.skills[skill_id] = original
        return True

    def install(self, url: str) -> Skill:
        """Fetch a skill from a link and save it. GitHub page links are converted."""
        text = fetch_text(url)
        return self.save(text)

    def install_file(self, filename: str, data: bytes) -> Tuple[List[Skill], List[str]]:
        """Add skills from a file the user picked: one ``.md``, or a ``.zip`` of them.

        Returns what was installed and what was ignored. A zip is read, never
        executed: entries are held to a size and count budget, anything that is
        not a plain markdown file inside the archive is skipped, and no path
        from the archive is ever used to write — each skill is written under a
        name derived from its own header.
        """
        name = (filename or "").strip().lower()
        if len(data) > MAX_UPLOAD:
            raise SkillError(f"the file must be under {MAX_UPLOAD // 1000} KB")
        if name.endswith(".zip") or data[:2] == b"PK":
            return self._install_zip(data)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillError("that file is not text — upload a .md file or a .zip of them") from exc
        return [self.save(text, Path(name).stem or None)], []

    def _install_zip(self, data: bytes) -> Tuple[List[Skill], List[str]]:
        import io
        import zipfile

        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise SkillError("that zip could not be read") from exc
        installed: List[Skill] = []
        ignored: List[str] = []
        total = 0
        with archive:
            entries = archive.infolist()[:MAX_ZIP_ENTRIES]
            if sum(e.file_size for e in entries) > MAX_UNPACKED:
                raise SkillError("that zip unpacks to too much — skills are markdown files")
            for entry in entries:
                label = entry.filename.rsplit("/", 1)[-1]
                if entry.is_dir() or entry.flag_bits & 0x1 or label.startswith((".", "__")):
                    continue
                if not label.lower().endswith(".md") or entry.file_size > MAX_FILE:
                    ignored.append(label)
                    continue
                try:
                    text = archive.read(entry)[:MAX_FILE].decode("utf-8")
                except (UnicodeDecodeError, zipfile.BadZipFile, RuntimeError):
                    ignored.append(label)
                    continue
                total += 1
                try:
                    # The id comes from the skill's own header, never from a
                    # path inside the archive.
                    installed.append(self.save(text, Path(label).stem or None))
                except SkillError:
                    ignored.append(label)
        if not installed:
            raise SkillError("no skill in that zip had a header with a description" if total
                             else "that zip has no .md files in it")
        return installed, ignored


class SkillError(ValueError):
    pass


MAX_FILE = 60_000
# A pack of skills is still just markdown: these bound what an upload can do.
MAX_UPLOAD = 2_000_000
MAX_UNPACKED = 8_000_000
MAX_ZIP_ENTRIES = 400
GITHUB_BLOB = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/blob/(.+)$")


def fetch_text(url: str) -> str:
    """Download a skill file, with the same host rules as every other fetch."""
    import urllib.parse
    import urllib.request

    from .tools import _is_public, safe_opener

    url = (url or "").strip()
    match = GITHUB_BLOB.match(url)
    if match:
        user, repo, rest = match.groups()
        url = f"https://raw.githubusercontent.com/{user}/{repo}/{rest}"
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SkillError("only http and https links can be installed")
    if not parsed.hostname or not _is_public(parsed.hostname):
        raise SkillError("that link points somewhere private")
    request = urllib.request.Request(url, headers={"User-Agent": "JEVia/1.0"})
    try:
        with safe_opener().open(request, timeout=20) as response:
            final = urllib.parse.urlparse(response.geturl())
            if final.hostname and not _is_public(final.hostname):
                raise SkillError("that link redirects somewhere private")
            raw = response.read(MAX_FILE + 1)
    except Exception as exc:  # noqa: BLE001
        raise SkillError(f"could not download it: {type(exc).__name__}") from exc
    if len(raw) > MAX_FILE:
        raise SkillError(f"a skill must be under {MAX_FILE // 1000} KB")
    return raw.decode("utf-8", errors="replace")
