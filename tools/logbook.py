#!/usr/bin/env python3
"""Check and build the logbook: a numbered, evidence-backed record that CVs are compiled from.

Standard library only (Python 3.11+), so any person or agent on any machine can run it without
installing anything.

    python tools/logbook.py check              validate every entry; fail if README or header SVGs are stale
    python tools/logbook.py build [--pdf]      regenerate README + header SVGs, and the CVs in out/
    python tools/logbook.py new KIND SLUG      start a draft entry with the next free number
    python tools/logbook.py hash FILE...       SHA-256 of an evidence file, for `document` evidence

The rules this enforces are written out for people and agents in AGENTS.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRY_DIR = ROOT / "entries"
VIEW_DIRS = (ROOT / "views", ROOT / "private" / "views")
PROFILE = ROOT / "profile.toml"
PRIVATE_PROFILE = ROOT / "private" / "profile.toml"
SKILLS = ROOT / "skills.toml"
README = ROOT / "README.md"
ASSETS = ROOT / "assets"
DOCS = ROOT / "docs"
OUT = ROOT / "out"
BEGIN, END = "<!-- logbook:begin -->", "<!-- logbook:end -->"
LANGS = ("en", "tr")

KINDS = {
    "award":       {"en": "award",       "tr": "ödül"},
    "project":     {"en": "project",     "tr": "proje"},
    "research":    {"en": "research",    "tr": "araştırma"},
    "certificate": {"en": "certificate", "tr": "sertifika"},
    "course":      {"en": "course",      "tr": "kurs"},
    "education":   {"en": "education",   "tr": "eğitim"},
    "experience":  {"en": "experience",  "tr": "deneyim"},
    "activity":    {"en": "activity",    "tr": "etkinlik"},
    "publication": {"en": "publication", "tr": "yayın"},
    "talk":        {"en": "talk",        "tr": "konuşma"},
}
STATUSES = ("draft", "approved", "retired")

# Strongest first. An entry is as strong as its strongest piece of evidence.
LEVELS = ("verifiable", "artifact", "document", "self")
MARK = {"verifiable": "●", "artifact": "◆", "document": "◧", "self": "○"}
LEVEL_NAME = {
    "en": {"verifiable": "verifiable", "artifact": "artifact", "document": "document",
           "self": "self-reported"},
    "tr": {"verifiable": "doğrulanabilir", "artifact": "yapıt", "document": "belge",
           "self": "beyan"},
}
GROUP_NAME = {"languages": "Languages", "ml": "Machine learning", "systems": "Systems",
              "web": "Web", "data": "Data", "tools": "Tools"}

ENTRY_KEYS = {"kind", "status", "date", "start", "ongoing", "expected", "title", "org", "role",
              "summary", "lines", "skills", "related", "supersedes", "notes", "evidence"}
PRESENT = {"en": "present", "tr": "devam ediyor"}
EXPECTED = {"en": "expected", "tr": "tahmini bitiş"}
EVIDENCE_KEYS = {"level", "label", "url", "file", "sha256"}
ENTRY_FILE = re.compile(r"^(\d{4})-([a-z0-9]+(?:-[a-z0-9]+)*)\.toml$")
SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DATE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
MAX_LINE = 240  # characters; a CV line longer than this wraps to a third row on A4


# ---- loading ------------------------------------------------------------------------------------

def load_toml(path: Path) -> dict:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"{rel(path)}: not valid TOML: {exc}") from None


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def text(value, lang: str) -> str:
    """A field is either one string for both languages or a table keyed by language."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get(lang) or ""
    return str(value)


@dataclass
class Entry:
    path: Path
    num: str
    slug: str
    data: dict

    @property
    def rel(self) -> str:
        return rel(self.path)

    @property
    def status(self) -> str:
        return self.data.get("status", "")

    @property
    def kind(self) -> str:
        return self.data.get("kind", "")

    def t(self, key: str, lang: str) -> str:
        return text(self.data.get(key), lang).strip()

    def lines(self, lang: str) -> list[str]:
        value = self.data.get("lines") or {}
        got = value.get(lang) if isinstance(value, dict) else None
        return [s.strip() for s in got] if isinstance(got, list) else []

    @property
    def evidence(self) -> list[dict]:
        ev = self.data.get("evidence") or []
        return ev if isinstance(ev, list) else []

    @property
    def level(self) -> str | None:
        found = [e.get("level") for e in self.evidence if e.get("level") in LEVELS]
        return min(found, key=LEVELS.index) if found else None

    @property
    def ongoing(self) -> bool:
        return self.data.get("ongoing") is True

    @property
    def date_key(self) -> tuple[int, int, int]:
        """When it happened; for something still going on, when it started."""
        return date_key(self.data.get("date") or self.data.get("start", ""))

    @property
    def year(self) -> str:
        return f"{self.date_key[0]:04d}"

    @property
    def sort_key(self):
        # Something still going on ranks as the most recent thing in the log.
        return (self.ongoing, self.date_key, self.num)


def date_key(s: str) -> tuple[int, int, int]:
    m = DATE.match(s or "")
    if not m:
        return (0, 0, 0)
    return (int(m[1]), int(m[2] or 0), int(m[3] or 0))


def load_entries() -> list[Entry]:
    entries = []
    for path in sorted(ENTRY_DIR.glob("*/*.toml")):
        m = ENTRY_FILE.match(path.name)
        if not m:
            raise SystemExit(f"{rel(path)}: entry files are named NNNN-slug.toml")
        entries.append(Entry(path, m[1], m[2], load_toml(path)))
    stray = [p for p in ENTRY_DIR.glob("*.toml")]
    if stray:
        raise SystemExit(f"{rel(stray[0])}: entries live in a year folder, entries/YYYY/")
    return entries


def load_private() -> dict:
    return load_toml(PRIVATE_PROFILE) if PRIVATE_PROFILE.exists() else {}


# ---- checking -----------------------------------------------------------------------------------

def check_date(where: str, field: str, s, today: dt.date, errors: list[str]) -> None:
    if not isinstance(s, str) or not DATE.match(s):
        errors.append(f"{where}: `{field}` must be YYYY, YYYY-MM or YYYY-MM-DD, got {s!r}")
        return
    y, m, d = date_key(s)
    try:
        dt.date(y, m or 1, d or 1)
    except ValueError:
        errors.append(f"{where}: `{field}` {s!r} is not a real date")
        return
    if (y, m, d) > (today.year, today.month if m else 0, today.day if d else 0):
        errors.append(f"{where}: `{field}` {s!r} is in the future; log things that happened")


def check_evidence(where: str, ev: dict, i: int, errors: list[str], notes: list[str]) -> None:
    at = f"{where}: evidence #{i + 1}"
    if not isinstance(ev, dict):
        errors.append(f"{at} must be a table")
        return
    for key in set(ev) - EVIDENCE_KEYS:
        errors.append(f"{at}: unknown key `{key}`")
    level = ev.get("level")
    if level not in LEVELS:
        errors.append(f"{at}: `level` must be one of {', '.join(LEVELS)}")
        return
    if not text(ev.get("label"), "en").strip():
        errors.append(f"{at}: every piece of evidence needs a `label` saying what it is")
    url = ev.get("url")
    if level in ("verifiable", "artifact"):
        if not (isinstance(url, str) and url.startswith("https://")):
            errors.append(f"{at}: {level} evidence needs an https:// `url`")
    if level == "document":
        f, h = ev.get("file"), ev.get("sha256")
        if not (isinstance(f, str) and f):
            errors.append(f"{at}: document evidence needs `file`")
            return
        if not (isinstance(h, str) and HEX64.match(h)):
            errors.append(f"{at}: document evidence needs `sha256`, 64 lowercase hex digits")
            return
        top = Path(f).parts[0] if Path(f).parts else ""
        if top not in ("private", "evidence") or ".." in Path(f).parts:
            errors.append(f"{at}: `file` must sit under private/ (kept off GitHub) or evidence/")
            return
        p = ROOT / f
        if p.exists():
            if sha256(p) != h:
                errors.append(f"{at}: {f} no longer matches its recorded sha256")
        elif top == "evidence":
            errors.append(f"{at}: {f} is missing")
        else:
            notes.append(f"{at}: {f} is not on this machine, so its hash was not checked")


def validate(entries: list[Entry], skills: dict, today: dt.date):
    errors: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []
    seen: dict[str, Entry] = {}
    for e in entries:
        where, d = e.rel, e.data
        if e.num in seen:
            errors.append(f"{where}: number {e.num} is already used by {seen[e.num].rel}")
        seen[e.num] = e
        for key in set(d) - ENTRY_KEYS:
            errors.append(f"{where}: unknown key `{key}` (allowed: {', '.join(sorted(ENTRY_KEYS))})")
        if e.kind not in KINDS:
            errors.append(f"{where}: `kind` must be one of {', '.join(KINDS)}")
        if e.status not in STATUSES:
            errors.append(f"{where}: `status` must be one of {', '.join(STATUSES)}")
        if "ongoing" in d and not isinstance(d["ongoing"], bool):
            errors.append(f"{where}: `ongoing` is true or false")
        if e.ongoing:
            if "start" not in d:
                errors.append(f"{where}: an ongoing entry needs `start`")
            if "date" in d:
                errors.append(f"{where}: an ongoing entry has no `date` yet; when it ends, "
                              f"set `date` and remove `ongoing`")
            if "expected" in d and not (isinstance(d["expected"], str) and DATE.match(d["expected"])):
                errors.append(f"{where}: `expected` must be YYYY, YYYY-MM or YYYY-MM-DD")
        else:
            check_date(where, "date", d.get("date"), today, errors)
            if "expected" in d:
                errors.append(f"{where}: `expected` only applies to an ongoing entry")
        if "start" in d:
            check_date(where, "start", d["start"], today, errors)
            if isinstance(d.get("date"), str) and date_key(d["start"]) > date_key(d["date"]):
                errors.append(f"{where}: `start` is after `date`")
        if e.date_key != (0, 0, 0) and e.path.parent.name != e.year:
            errors.append(f"{where}: file is in {e.path.parent.name}/ but the entry belongs to {e.year}")

        approved = e.status == "approved"
        if not e.t("title", "en"):
            errors.append(f"{where}: `title.en` is required")
        for lang in LANGS:
            for key in ("title", "summary"):
                if not e.t(key, lang):
                    (errors if approved else warnings).append(
                        f"{where}: `{key}.{lang}` is {'required for an approved entry' if approved else 'still empty'}")
        lines = d.get("lines")
        if lines is not None:
            if not isinstance(lines, dict) or any(not isinstance(lines.get(k, []), list) for k in lines):
                errors.append(f"{where}: `lines` is a table of string lists: lines.en = [...], lines.tr = [...]")
            else:
                if bool(e.lines("en")) != bool(e.lines("tr")):
                    warnings.append(f"{where}: `lines` exist in one language only")
                for lang in LANGS:
                    for s in e.lines(lang):
                        if len(s) > MAX_LINE:
                            warnings.append(f"{where}: a `lines.{lang}` line is {len(s)} characters; "
                                            f"over {MAX_LINE} it stops reading as one CV line")
        for slug in d.get("skills", []):
            if slug not in skills:
                errors.append(f"{where}: skill `{slug}` is not in skills.toml; add it there first")
        if not e.evidence:
            (errors if approved else warnings).append(
                f"{where}: no evidence; even a self-reported entry says so with level = \"self\"")
        for i, ev in enumerate(e.evidence):
            check_evidence(where, ev, i, errors, notes)

    for e in entries:
        refs = list(e.data.get("related", []))
        if e.data.get("supersedes"):
            refs.append(e.data["supersedes"])
        for r in refs:
            if r == e.num:
                errors.append(f"{e.rel}: refers to itself")
            elif r not in seen:
                errors.append(f"{e.rel}: refers to entry {r!r}, which does not exist (use the 4-digit number)")
    nums = sorted(int(n) for n in seen)
    if nums and nums != list(range(1, nums[-1] + 1)):
        warnings.append("entry numbers have gaps; numbers are never reused, but a gap usually means a lost file")
    return errors, warnings, notes


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def public_files() -> list[Path]:
    """Files that are, or are about to be, published: tracked plus untracked-but-not-ignored."""
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                             cwd=ROOT, capture_output=True, check=True).stdout.decode()
        return [ROOT / p for p in out.split("\0") if p]
    except (OSError, subprocess.CalledProcessError):
        skip = {"private", "out", "inbox", ".git"}
        return [p for p in ROOT.rglob("*") if p.is_file() and not (set(p.relative_to(ROOT).parts) & skip)]


def privacy_errors(private: dict) -> list[str]:
    errors = []
    for p in public_files():
        top = p.relative_to(ROOT).parts[0]
        if top in ("private", "out") or (top == "inbox" and p.name != "README.md"):
            errors.append(f"{rel(p)} would be published; private/, out/ and inbox/ must stay out of git")
    secrets = {}
    for key in ("name", "email", "phone", "address", "birth_date", "national_id"):
        v = private.get(key)
        if isinstance(v, str) and len(v.strip()) >= 3:
            secrets[key] = v.strip().casefold()
    name = private.get("name")
    if isinstance(name, str) and len(name.split()) > 1 and len(name.split()[-1]) >= 3:
        secrets["surname"] = name.split()[-1].casefold()
    if not secrets:
        return errors
    for p in public_files():
        if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".pdf", ".gif", ".webp") or not p.exists():
            continue
        try:
            body = p.read_text(encoding="utf-8").casefold()
        except (UnicodeDecodeError, OSError):
            continue
        for key, value in secrets.items():
            if value in body:
                # Never print the value itself: this message may end up in a public CI log.
                errors.append(f"{rel(p)}: contains your private `{key}` from private/profile.toml")
    return errors


# ---- the README block and the header SVG --------------------------------------------------------

def cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ").strip()


def when_full(e: Entry, lang: str = "en") -> str:
    s = e.data.get("start")
    if e.ongoing:
        exp = e.data.get("expected")
        return f"{s} → {PRESENT[lang]}" + (f" ({EXPECTED[lang]} {exp})" if exp else "")
    d = e.data.get("date", "")
    return f"{s} → {d}" if s and s != d else d


def when_short(e: Entry) -> str:
    start = (e.data.get("start") or "")[:7]
    if e.ongoing:
        return f"{start} →"
    end = e.data.get("date", "")[:7]
    if not start or start == end:
        return end
    if start[:4] == end[:4] and len(end) == 7:
        return f"{start}–{end[5:]}"
    return f"{start}–{end}"


def render_block(entries: list[Entry], skills: dict, profile: dict) -> str:
    shown = sorted((e for e in entries if e.status == "approved"), key=lambda e: e.sort_key, reverse=True)
    drafts = sum(e.status == "draft" for e in entries)
    counts = {lv: sum(e.level == lv for e in shown) for lv in LEVELS}
    out = [
        "<picture>",
        '  <source media="(prefers-color-scheme: dark)" srcset="assets/header-dark.svg">',
        f'  <img src="assets/header-light.svg" width="880" alt="{html.escape(svg_alt(shown, profile))}">',
        "</picture>",
        "",
        f"**Carried forward:** {len(shown)} {'entry' if len(shown) == 1 else 'entries'} · "
        + " · ".join(f"{MARK[lv]} {counts[lv]} {LEVEL_NAME['en'][lv]}" for lv in LEVELS)
        + (f". {drafts} draft{'s' if drafts != 1 else ''} await sign-off and are not shown." if drafts else "."),
    ]
    years = sorted({e.year for e in shown}, reverse=True)
    for year in years:
        out += ["", f"## {year}", "", "| No. | When | Entry | Evidence |", "|---|---|---|---|"]
        for e in (x for x in shown if x.year == year):
            org = e.t("org", "en")
            entry = f"**[{cell(e.t('title', 'en'))}]({e.rel})**" + (f" · {cell(org)}" if org else "")
            entry += f"<br><sub>{KINDS[e.kind]['en']} · {cell(e.t('summary', 'en'))}</sub>"
            ev = []
            for x in e.evidence:
                label = cell(text(x.get("label"), "en"))
                if x.get("level") in ("verifiable", "artifact"):
                    ev.append(f"{MARK[x['level']]} [{label}]({x['url']})")
                elif x.get("level") == "document":
                    ev.append(f"{MARK['document']} {label} <sub>`sha256 {x['sha256'][:12]}…`</sub>")
                else:
                    ev.append(f"{MARK['self']} {label}")
            out.append(f"| `{e.num}` | {when_full(e)} | {entry} | {'<br>'.join(ev)} |")

    index: dict[str, list[Entry]] = {}
    for e in shown:
        for s in e.data.get("skills", []):
            index.setdefault(s, []).append(e)
    if index:
        out += ["", "## Skills, by evidence", "",
                "No self-ratings. A skill is listed only once an approved entry shows it in use.", "",
                "| Skill | Area | Entries |", "|---|---|---|"]
        order = sorted(index, key=lambda s: (list(GROUP_NAME).index(skills[s].get("group", "tools"))
                                             if skills[s].get("group") in GROUP_NAME else 99,
                                             skills[s]["en"].casefold()))
        for s in order:
            refs = " ".join(f"[`{e.num}`]({e.rel})" for e in sorted(index[s], key=lambda e: e.num))
            out.append(f"| {skills[s]['en']} | {GROUP_NAME.get(skills[s].get('group'), '')} | {refs} |")
    return "\n".join(out)


def svg_alt(shown: list[Entry], profile: dict) -> str:
    if not shown:
        return f"Logbook of {profile.get('display_name', '')}: no entries yet"
    first = min(e.date_key for e in shown)
    last = max(e.date_key for e in shown)
    return (f"Logbook of {profile.get('display_name', '')}: {len(shown)} entries, "
            f"{first[0]}-{first[1]:02d} to {last[0]}-{last[1]:02d}")


THEMES = {
    "light": {"paper": "#f6f3ea", "ink": "#1d1b18", "muted": "#6b655c", "rule": "#cdc4b1",
              "accent": "#8c2f23"},
    "dark": {"paper": "#15171b", "ink": "#ebe6dc", "muted": "#9f998e", "rule": "#3b3e45",
             "accent": "#e3906f"},
}


def glyph(level: str, cx: float, cy: float, r: float, ink: str, paper: str) -> str:
    if level == "verifiable":
        return f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{ink}"/>'
    if level == "artifact":
        k = r * 1.25
        return f'<polygon points="{cx},{cy - k} {cx + k},{cy} {cx},{cy + k} {cx - k},{cy}" fill="{ink}"/>'
    if level == "document":
        s = r * 1.7
        x, y = cx - s / 2, cy - s / 2
        return (f'<rect x="{x}" y="{y}" width="{s}" height="{s}" fill="{paper}" stroke="{ink}" stroke-width="1.2"/>'
                f'<rect x="{x}" y="{y}" width="{s / 2}" height="{s}" fill="{ink}"/>')
    return f'<circle cx="{cx}" cy="{cy}" r="{r - 0.6}" fill="none" stroke="{ink}" stroke-width="1.2"/>'


def render_svg(entries: list[Entry], profile: dict, theme: str) -> str:
    c = THEMES[theme]
    shown = sorted((e for e in entries if e.status == "approved"), key=lambda e: e.sort_key)
    name = html.escape(profile.get("display_name", ""))
    headline = html.escape(text(profile.get("headline"), "en"))
    if shown:
        first, last = min(e.date_key for e in shown), max(e.date_key for e in shown)
        period = f"{first[0]}-{first[1]:02d} → {last[0]}-{last[1]:02d}"
        latest = max(e.num for e in shown)
    else:
        period, latest = "—", "—"
    W, H = 880, 206
    serif = "'Iowan Old Style', Charter, 'Sitka Text', Cambria, Georgia, serif"
    mono = "ui-monospace, 'Cascadia Mono', SFMono-Regular, Menlo, Consolas, monospace"
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
        f'role="img" aria-label="{html.escape(svg_alt(shown, profile))}">',
        f"<style>.s{{font-family:{serif}}}.m{{font-family:{mono}}}</style>",
        f'<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="7" fill="{c["paper"]}" stroke="{c["rule"]}"/>',
        f'<rect x="8.5" y="8.5" width="{W - 17}" height="{H - 17}" rx="3" fill="none" stroke="{c["rule"]}" stroke-width="0.8"/>',
        f'<text class="m" x="34" y="46" font-size="11" letter-spacing="3.2" fill="{c["accent"]}">LOGBOOK</text>',
        f'<text class="s" x="32" y="90" font-size="38" fill="{c["ink"]}">{name}</text>',
        f'<text class="s" x="34" y="117" font-size="15" font-style="italic" fill="{c["muted"]}">{headline}</text>',
    ]
    facts = [("ENTRIES", str(len(shown))), ("PERIOD", period), ("LAST ENTRY", f"No. {latest}")]
    for i, (label, value) in enumerate(facts):
        y = 48 + i * 26
        parts.append(f'<text class="m" x="606" y="{y}" font-size="9.5" letter-spacing="2" fill="{c["muted"]}">{label}</text>')
        parts.append(f'<text class="m" x="846" y="{y}" font-size="13" text-anchor="end" fill="{c["ink"]}">{html.escape(value)}</text>')
    parts.append(f'<line x1="34" y1="138" x2="846" y2="138" stroke="{c["rule"]}" stroke-width="0.8"/>')

    # The strip: one cell per approved entry, oldest on the left, marked by its evidence level.
    cell_w, x0, legend_x = 32, 34, 500
    room = int((legend_x - 24 - x0) // cell_w)
    strip = shown[-room:] if len(shown) > room else shown
    x = x0
    if len(shown) > room:
        parts.append(f'<text class="m" x="{x}" y="176" font-size="9" fill="{c["muted"]}">+{len(shown) - room}</text>')
        x += cell_w
    for e in strip:
        parts.append(glyph(e.level or "self", x + cell_w / 2 - 6, 162, 5, c["ink"], c["paper"]))
        parts.append(f'<text class="m" x="{x + cell_w / 2 - 6}" y="186" font-size="8.5" text-anchor="middle" fill="{c["muted"]}">{e.num}</text>')
        x += cell_w
    # Right-aligned to the inner rule. Monospace advance is ~0.62em in every font in the stack.
    char_w, gap, pad = 9.5 * 0.62, 16, 15
    widths = [pad + len(LEVEL_NAME["en"][lv]) * char_w for lv in LEVELS]
    lx = 846 - sum(widths) - gap * (len(LEVELS) - 1)
    for lv, w in zip(LEVELS, widths):
        parts.append(glyph(lv, lx + 5, 166, 4.2, c["ink"], c["paper"]))
        parts.append(f'<text class="m" x="{lx + pad}" y="169.5" font-size="9.5" fill="{c["muted"]}">{LEVEL_NAME["en"][lv]}</text>')
        lx += w + gap
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def splice_readme(current: str, block: str) -> str:
    if BEGIN not in current or END not in current:
        raise SystemExit(f"README.md must contain {BEGIN} and {END}; the generated part goes between them")
    head, rest = current.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    return f"{head}{BEGIN}\n{block}\n{END}{tail}"


def public_outputs(entries, skills, profile) -> dict[Path, str]:
    current = README.read_text(encoding="utf-8") if README.exists() else f"{BEGIN}\n{END}\n"
    return {
        README: splice_readme(current, render_block(entries, skills, profile)),
        ASSETS / "header-light.svg": render_svg(entries, profile, "light"),
        ASSETS / "header-dark.svg": render_svg(entries, profile, "dark"),
        DOCS / "index.html": render_site("en", entries, skills, profile),
        DOCS / "tr" / "index.html": render_site("tr", entries, skills, profile),
        DOCS / ".nojekyll": "",
    }


# ---- CVs ----------------------------------------------------------------------------------------

UI = {
    "en": {
        "kicker": "Logbook extract",
        "provenance": "Every entry below is numbered and backed by evidence in a public logbook:",
        "skills": "Evidenced skills",
        "compiled": "Compiled from the logbook on {date}. Self-ratings are not used; a skill appears only where an entry shows it.",
    },
    "tr": {
        "kicker": "Seyir defterinden döküm",
        "provenance": "Buradaki her kayıt numaralı ve kanıtıyla birlikte herkese açık bir defterde duruyor:",
        "skills": "Kanıtlı yetenekler",
        "compiled": "Defterden {date} tarihinde derlendi. Kendi verdiğim puanlar yok; bir yetenek ancak onu gösteren bir kayıt varsa listede.",
    },
}

CV_CSS = """
@page { size: A4; margin: 15mm 16mm 14mm; }
:root { --ink:#16181b; --muted:#5d6166; --rule:#cbc5b8; --accent:#7d2b22;
        --serif:"Sitka Text","Charter","Iowan Old Style",Cambria,Georgia,serif;
        --mono:"Cascadia Mono","JetBrains Mono",Consolas,"SFMono-Regular",Menlo,monospace; }
* { box-sizing: border-box; }
html { font-size: 9.6pt; }
body { margin: 0; color: var(--ink); background: #fff; font-family: var(--serif); line-height: 1.38;
       font-variant-numeric: lining-nums;
       -webkit-print-color-adjust: exact; print-color-adjust: exact; }
a { color: inherit; text-decoration: none; }
.kicker { font-family: var(--mono); font-size: 7.4pt; letter-spacing: .04em; text-transform: uppercase;
          color: var(--accent); display: flex; justify-content: space-between; }
h1 { font-size: 23pt; font-weight: 600; letter-spacing: -.01em; margin: 2.2mm 0 0; line-height: 1.1; }
.headline { font-style: italic; color: var(--muted); margin: .8mm 0 0; font-size: 10.6pt; }
.contact { font-family: var(--mono); font-size: 7.8pt; margin: 2.4mm 0 0; }
.contact span + span::before, .extra span + span::before { content: "  /  "; color: var(--rule); white-space: pre; }
.extra { font-size: 8.4pt; color: var(--muted); margin: .8mm 0 0; }
.provenance { border-top: .7pt solid var(--ink); border-bottom: .5pt solid var(--rule); margin: 4mm 0 1mm;
              padding: 1.5mm 0; font-size: 7.8pt; color: var(--muted); display: flex; gap: 5mm;
              justify-content: space-between; align-items: center; }
.provenance .legend { font-family: var(--mono); white-space: nowrap; }
.provenance .legend span { margin-left: 2.4mm; }
section { margin-top: 4.2mm; }
h2 { font-family: var(--mono); font-size: 7.8pt; letter-spacing: .04em; text-transform: uppercase;
     font-weight: 600; color: var(--accent); margin: 0 0 1mm; display: flex; align-items: center; gap: 2.5mm; }
h2::after { content: ""; flex: 1; border-bottom: .5pt solid var(--rule); }
.entry { display: grid; grid-template-columns: 18mm 1fr 19mm; column-gap: 3mm; padding: 1.3mm 0;
         break-inside: avoid; }
.when { font-family: var(--mono); font-size: 7.8pt; color: var(--muted); padding-top: .55mm; }
.title { font-weight: 600; font-size: 10.2pt; }
.org { font-weight: 400; color: var(--muted); }
.summary { margin: .4mm 0 0; }
ul { margin: .6mm 0 0; padding-left: 3.4mm; }
li { margin: .35mm 0; }
li::marker { content: "–  "; color: var(--muted); }
.ref { font-family: var(--mono); font-size: 7.4pt; color: var(--muted); text-align: right;
       padding-top: .7mm; white-space: nowrap; }
svg.g { width: 7.5pt; height: 7.5pt; vertical-align: -.8pt; margin-right: 1.2mm; }
.skills { display: grid; grid-template-columns: 1fr 1fr; column-gap: 9mm; }
.skill { display: flex; justify-content: space-between; gap: 3mm; border-bottom: .4pt dotted var(--rule);
         padding: .7mm 0; }
.skill .refs { font-family: var(--mono); font-size: 7.4pt; color: var(--muted); white-space: nowrap; }
footer { margin-top: 5mm; font-family: var(--mono); font-size: 7pt; color: var(--muted); }
@media screen {
  body { background: #e8e4db; }
  main { background: #fff; width: 210mm; min-height: 297mm; margin: 8mm auto; padding: 15mm 16mm 14mm;
         box-shadow: 0 1px 4px rgba(0,0,0,.18); }
}
"""


def glyph_svg(level: str) -> str:
    inner = {
        "verifiable": '<circle cx="5" cy="5" r="4.2" fill="currentColor"/>',
        "artifact": '<polygon points="5,0.4 9.6,5 5,9.6 0.4,5" fill="currentColor"/>',
        "document": ('<rect x="1" y="1" width="8" height="8" fill="none" stroke="currentColor" stroke-width="1.1"/>'
                     '<rect x="1" y="1" width="4" height="8" fill="currentColor"/>'),
        "self": '<circle cx="5" cy="5" r="3.8" fill="none" stroke="currentColor" stroke-width="1.1"/>',
    }[level]
    return f'<svg class="g" viewBox="0 0 10 10" aria-hidden="true">{inner}</svg>'


def select(view: dict, entries: list[Entry]) -> list[Entry]:
    only = set(view.get("only", []))
    exclude = set(view.get("exclude", []))
    floor = LEVELS.index(view.get("min_level", "self"))
    return [e for e in entries
            if e.status == "approved" and (not only or e.num in only) and e.num not in exclude
            and e.level is not None and LEVELS.index(e.level) <= floor]


def ordered(view: dict, chosen: list[Entry]) -> list[Entry]:
    pin = view.get("pin", [])
    return sorted(chosen, key=lambda e: (pin.index(e.num) if e.num in pin else len(pin),
                                         not e.ongoing, tuple(-x for x in e.date_key), e.num))


def cv_sections(view: dict, entries: list[Entry]) -> list[tuple[dict, list[Entry]]]:
    """The sections a view prints and the entries in each, after selection, order and limits."""
    chosen = select(view, entries)
    out = []
    for sec in view.get("section", []):
        picked = ordered(view, [e for e in chosen if e.kind in sec.get("kinds", [])])
        if sec.get("limit"):
            picked = picked[: sec["limit"]]
        if picked:
            out.append((sec, picked))
    return out


def entry_url(profile: dict, e: Entry) -> str:
    return f"{profile.get('repo', '').rstrip('/')}/blob/main/{e.rel}"


def render_cv(view: dict, entries: list[Entry], skills: dict, profile: dict, private: dict,
              today: dt.date) -> str:
    lang = view.get("lang", "en")
    ui = UI[lang]
    esc = html.escape
    chosen = select(view, entries)
    name = private.get("name") or profile.get("display_name", "")
    links = profile.get("links", {})
    contact = []
    if private.get("email"):
        contact.append(f'<a href="mailto:{esc(private["email"])}">{esc(private["email"])}</a>')
    if private.get("phone"):
        contact.append(esc(private["phone"]))
    if text(private.get("location"), lang):
        contact.append(esc(text(private.get("location"), lang)))
    for url in (links.get("github"), private.get("linkedin") or links.get("linkedin"), links.get("website")):
        if url:
            contact.append(f'<a href="{esc(url)}">{esc(url.removeprefix("https://").removeprefix("www."))}</a>')
    extra = (private.get("extra") or {}).get(lang) or {}
    repo = profile.get("repo", "")
    legend = "".join(f"<span>{glyph_svg(lv)}{LEVEL_NAME[lang][lv]}</span>" for lv in LEVELS)

    body = [
        f'<div class="kicker"><span>{esc(ui["kicker"])}</span><span>{today.isoformat()}</span></div>',
        f"<h1>{esc(name)}</h1>",
        f'<p class="headline">{esc(text(profile.get("headline"), lang))}</p>',
        f'<p class="contact">{"".join(f"<span>{c}</span>" for c in contact)}</p>' if contact else "",
        (f'<p class="extra">{"".join(f"<span>{esc(k)}: {esc(str(v))}</span>" for k, v in extra.items())}</p>'
         if extra else ""),
        f'<div class="provenance"><span>{esc(ui["provenance"])} '
        f'<a href="{esc(repo)}">{esc(repo.removeprefix("https://"))}</a></span>'
        f'<span class="legend">{legend}</span></div>',
    ]
    used: set[str] = set()
    for sec, picked in cv_sections(view, entries):
        used.update(e.num for e in picked)
        body.append(f"<section><h2>{esc(text(sec.get('heading'), lang))}</h2>")
        for e in picked:
            bits = [x for x in (e.t("org", lang), e.t("role", lang), expected_note(e, lang)) if x]
            head = f'<span class="title">{esc(e.t("title", lang))}</span>'
            if bits:
                head += f'<span class="org"> · {esc(" · ".join(bits))}</span>'
            lines = e.lines(lang)
            detail = ("<ul>" + "".join(f"<li>{esc(s)}</li>" for s in lines) + "</ul>") if lines \
                else f'<p class="summary">{esc(e.t("summary", lang))}</p>'
            body.append(
                f'<article class="entry"><div class="when">{esc(when_short(e))}</div>'
                f"<div>{head}{detail}</div>"
                f'<div class="ref">{glyph_svg(e.level)}<a href="{esc(entry_url(profile, e))}">No. {e.num}</a></div>'
                f"</article>")
        body.append("</section>")

    if view.get("show_skills", True):
        index: dict[str, list[str]] = {}
        for e in chosen:
            if e.num in used:
                for s in e.data.get("skills", []):
                    index.setdefault(s, []).append(e.num)
        if index:
            body.append(f"<section><h2>{esc(ui['skills'])}</h2><div class=\"skills\">")
            for s in sorted(index, key=lambda s: (-len(index[s]), skills[s][lang].casefold())):
                body.append(f'<div class="skill"><span>{esc(skills[s][lang])}</span>'
                            f'<span class="refs">{" ".join(sorted(index[s]))}</span></div>')
            body.append("</div></section>")
    body.append(f"<footer>{esc(ui['compiled'].format(date=today.isoformat()))}</footer>")
    title = f"{name} · {text(view.get('title'), lang) or 'CV'}"
    return (f'<!doctype html>\n<html lang="{lang}">\n<head>\n<meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f"<title>{esc(title)}</title>\n<style>{CV_CSS}</style>\n</head>\n<body>\n<main>\n"
            + "\n".join(x for x in body if x) + "\n</main>\n</body>\n</html>\n")


def render_forms(view: dict, entries: list[Entry], profile: dict) -> str:
    """Plain text for application forms that make you retype everything into boxes."""
    lang = view.get("lang", "en")
    blocks = []
    for e in ordered(view, select(view, entries)):
        org = e.t("org", lang)
        head = [e.t("title", lang), f"{when_short(e)}" + (f" · {org}" if org else "")]
        body = [e.t("summary", lang)] + [f"- {s}" for s in e.lines(lang)]
        ev = [x["url"] for x in e.evidence if x.get("url")]
        tail = ([f"Evidence: {u}" for u in ev] + [f"Logbook: {entry_url(profile, e)}"])
        blocks.append("\n".join(head + body + tail))
    return "\n\n\n".join(blocks) + "\n"


def expected_note(e: Entry, lang: str) -> str:
    return f"{EXPECTED[lang]} {e.data['expected']}" if e.ongoing and e.data.get("expected") else ""


def make_pack(stem: str, view: dict, entries: list[Entry]) -> str:
    """The CV PDF plus copies of the private documents behind the entries it shows: one folder to
    attach to an application that asks for certificates."""
    pack = OUT / f"{stem}-pack"
    if pack.exists():
        shutil.rmtree(pack)
    pack.mkdir(parents=True)
    shutil.copy2(OUT / f"{stem}.pdf", pack / f"{stem}.pdf")
    n = 0
    for _, picked in cv_sections(view, entries):
        for e in picked:
            for ev in e.evidence:
                src = ROOT / ev.get("file", "") if ev.get("level") == "document" else None
                if src and src.is_file():
                    shutil.copy2(src, pack / f"{e.num}-{src.name}")
                    n += 1
    return f"out/{stem}-pack/ (CV + {n} document{'' if n == 1 else 's'})"


# ---- the site (GitHub Pages, from docs/) --------------------------------------------------------

SITE_UI = {
    "en": {"kicker": "Logbook", "other": ("tr", "Türkçe", "tr/"), "entries": "entries",
           "skills": "Skills, by evidence", "source": "source",
           "skills_note": "No self-ratings: a skill is listed only where an entry shows it in use.",
           "intro": "Numbered entries, each with its evidence. The CVs I send are compiled from them.",
           "footer": "Compiled from the entries in"},
    "tr": {"kicker": "Seyir defteri", "other": ("en", "English", "../"), "entries": "kayıt",
           "skills": "Kanıtlı yetenekler", "source": "kaynak",
           "skills_note": "Kendi verdiğim puan yok. Bir yetenek ancak onu kullandığım bir kayıt varsa listede.",
           "intro": "Her biri kanıtıyla birlikte numaralı kayıtlar. Gönderdiğim CV'ler bunlardan derleniyor.",
           "footer": "Şu repodaki kayıtlardan derlendi:"},
}

SITE_CSS = """
:root { --paper:#f6f3ea; --ink:#1d1b18; --muted:#6b655c; --rule:#d3cab7; --accent:#8c2f23;
  --serif:"Iowan Old Style","Charter","Sitka Text",Cambria,Georgia,serif;
  --mono:ui-monospace,"Cascadia Mono","SFMono-Regular",Menlo,Consolas,monospace; color-scheme: light; }
@media (prefers-color-scheme: dark) {
  :root { --paper:#15171b; --ink:#ebe6dc; --muted:#a39d92; --rule:#3a3e46; --accent:#e3906f;
          color-scheme: dark; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--paper); color: var(--ink); font-family: var(--serif);
       font-size: 17px; line-height: 1.5; font-variant-numeric: lining-nums; }
a { color: inherit; text-decoration-color: var(--rule); text-underline-offset: 3px; }
a:hover { text-decoration-color: var(--accent); }
main { max-width: 860px; margin: 0 auto; padding: 48px 16px 64px; }
.top { display: flex; justify-content: space-between; align-items: baseline; font-family: var(--mono);
       font-size: 12px; letter-spacing: .14em; text-transform: uppercase; color: var(--accent); }
.top a { color: var(--muted); letter-spacing: .06em; }
h1 { font-size: clamp(36px, 8vw, 56px); font-weight: 600; margin: 14px 0 0; line-height: 1.05;
     letter-spacing: -.01em; overflow-wrap: anywhere; }
.headline { font-style: italic; color: var(--muted); margin: 6px 0 0; font-size: 19px; }
.links { font-family: var(--mono); font-size: 13px; margin: 12px 0 0; overflow-wrap: anywhere; }
.intro { margin: 22px 0 0; max-width: 60ch; }
.ledger { margin: 26px 0 0; border-top: 1.5px solid var(--ink); border-bottom: 1px solid var(--rule);
          padding: 10px 0; display: flex; flex-wrap: wrap; gap: 6px 22px; font-family: var(--mono);
          font-size: 12.5px; color: var(--muted); }
.ledger b { color: var(--ink); font-weight: 600; }
svg.g { width: 10px; height: 10px; color: var(--ink); margin-right: 6px; vertical-align: -1px; }
section { margin-top: 40px; }
h2 { font-family: var(--mono); font-size: 12.5px; letter-spacing: .14em; text-transform: uppercase;
     color: var(--accent); font-weight: 600; display: flex; align-items: center; gap: 12px; margin: 0 0 4px; }
h2::after { content: ""; flex: 1; border-bottom: 1px solid var(--rule); }
article { display: grid; grid-template-columns: 116px 1fr; gap: 0 22px; padding: 16px 0;
          border-bottom: 1px dotted var(--rule); scroll-margin-top: 16px; }
article:last-child { border-bottom: 0; }
article:target h3 { text-decoration: underline 2px var(--accent); text-underline-offset: 5px; }
.when { font-family: var(--mono); font-size: 13px; color: var(--muted); padding-top: 4px; }
.when .no { display: block; margin-top: 4px; font-size: 12px; }
h3 { margin: 0; font-size: 20px; font-weight: 600; line-height: 1.3; }
.org { color: var(--muted); font-size: 16px; margin-top: 2px; }
.summary { margin: 8px 0 0; }
ul.lines { margin: 8px 0 0; padding-left: 18px; }
ul.lines li { margin: 4px 0; }
ul.lines li::marker { content: "–  "; color: var(--muted); }
ul.evidence { list-style: none; padding: 0; margin: 12px 0 0; font-family: var(--mono); font-size: 13px;
              color: var(--muted); }
ul.evidence li { margin: 3px 0; overflow-wrap: anywhere; }
ul.evidence code { font-size: 12px; }
.used { font-family: var(--mono); font-size: 12px; color: var(--muted); margin-top: 8px; }
.note { color: var(--muted); font-size: 15px; margin: 6px 0 10px; }
.skills { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 0 28px; }
.skill { display: flex; justify-content: space-between; gap: 12px; padding: 7px 0;
         border-bottom: 1px dotted var(--rule); }
.skill span:last-child { font-family: var(--mono); font-size: 13px; color: var(--muted); }
.skill a { text-decoration: none; }
footer { margin-top: 48px; font-family: var(--mono); font-size: 12px; color: var(--muted); }
@media (max-width: 600px) {
  body { font-size: 16px; }
  main { padding-top: 32px; }
  article { grid-template-columns: 1fr; }
  .when { display: flex; flex-wrap: wrap; gap: 4px 14px; padding: 0 0 6px; }
  .when .no { margin: 0; }
}
"""


def site_sections(lang: str) -> list[dict]:
    """The site groups entries the way the public CV view of the same language does."""
    for p in sorted((ROOT / "views").glob("*.toml")):
        v = load_toml(p)
        if v.get("lang") == lang and v.get("section"):
            return v["section"]
    return [{"heading": k, "kinds": [k]} for k in KINDS]


def site_entry(e: Entry, lang: str, skills: dict, repo: str) -> str:
    esc = html.escape
    ui = SITE_UI[lang]
    bits = [x for x in (e.t("org", lang), e.t("role", lang), expected_note(e, lang)) if x]
    body = f'<p class="summary">{esc(e.t("summary", lang))}</p>'
    if e.lines(lang):
        body += '<ul class="lines">' + "".join(f"<li>{esc(x)}</li>" for x in e.lines(lang)) + "</ul>"
    items = []
    for x in e.evidence:
        lv = x.get("level")
        label = esc(text(x.get("label"), lang) or text(x.get("label"), "en"))
        if lv in ("verifiable", "artifact"):
            item = f'<a href="{esc(x["url"])}">{label}</a>'
        elif lv == "document":
            item = f'{label} <code>sha256 {esc(x["sha256"][:16])}…</code>'
        else:
            item = label
        items.append(f"<li>{glyph_svg(lv)}{item}</li>")
    used = " · ".join(esc(skills[s][lang]) for s in e.data.get("skills", []) if s in skills)
    return (f'<article id="e{e.num}"><div class="when"><span>{esc(when_short(e))}</span>'
            f'<span class="no"><a href="#e{e.num}">No. {e.num}</a> · '
            f'<a href="{esc(repo)}/blob/main/{e.rel}">{ui["source"]}</a></span></div>'
            f'<div><h3>{esc(e.t("title", lang))}</h3>'
            + (f'<div class="org">{esc(" · ".join(bits))}</div>' if bits else "")
            + body + f'<ul class="evidence">{"".join(items)}</ul>'
            + (f'<div class="used">{used}</div>' if used else "") + "</div></article>")


def render_site(lang: str, entries: list[Entry], skills: dict, profile: dict) -> str:
    """One public page per language, from approved entries only. Deterministic, so `check` can
    tell when it is stale: no build date, nothing from private/."""
    esc = html.escape
    ui = SITE_UI[lang]
    shown = [e for e in entries if e.status == "approved"]
    repo = profile.get("repo", "").rstrip("/")
    name = profile.get("display_name", "")
    other_lang, other_label, other_href = ui["other"]
    links = profile.get("links", {})
    link_html = "  /  ".join(f'<a href="{esc(u)}">{esc(u.removeprefix("https://"))}</a>'
                             for u in (links.get("github"), links.get("linkedin"), links.get("website")) if u)
    ledger = [f"<span><b>{len(shown)}</b> {ui['entries']}</span>"]
    for lv in LEVELS:
        ledger.append(f"<span>{glyph_svg(lv)}<b>{sum(e.level == lv for e in shown)}</b> "
                      f"{LEVEL_NAME[lang][lv]}</span>")
    parts = [
        f'<div class="top"><span>{esc(ui["kicker"])}</span>'
        f'<a href="{other_href}" hreflang="{other_lang}">{other_label}</a></div>',
        f"<h1>{esc(name)}</h1>",
        f'<p class="headline">{esc(text(profile.get("headline"), lang))}</p>',
        f'<p class="links">{link_html}</p>' if link_html else "",
        f'<p class="intro">{esc(ui["intro"])}</p>',
        f'<div class="ledger">{"".join(ledger)}</div>',
    ]
    for sec in site_sections(lang):
        picked = sorted((e for e in shown if e.kind in sec.get("kinds", [])),
                        key=lambda e: e.sort_key, reverse=True)
        if picked:
            parts.append(f"<section><h2>{esc(text(sec.get('heading'), lang))}</h2>")
            parts += [site_entry(e, lang, skills, repo) for e in picked]
            parts.append("</section>")
    index: dict[str, list[str]] = {}
    for e in sorted(shown, key=lambda e: e.num):
        for s in e.data.get("skills", []):
            index.setdefault(s, []).append(e.num)
    if index:
        parts.append(f"<section><h2>{esc(ui['skills'])}</h2><p class=\"note\">{esc(ui['skills_note'])}</p>"
                     '<div class="skills">')
        for s in sorted(index, key=lambda s: (-len(index[s]), skills[s][lang].casefold())):
            refs = " ".join(f'<a href="#e{n}">{n}</a>' for n in index[s])
            parts.append(f'<div class="skill"><span>{esc(skills[s][lang])}</span><span>{refs}</span></div>')
        parts.append("</div></section>")
    parts.append(f'<footer>{esc(ui["footer"])} <a href="{esc(repo)}">{esc(repo.removeprefix("https://"))}</a></footer>')
    desc = text(profile.get("headline"), lang)
    return (f'<!doctype html>\n<html lang="{lang}">\n<head>\n<meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f"<title>{esc(name)} · {esc(ui['kicker'])}</title>\n"
            f'<meta name="description" content="{esc(desc)}">\n'
            f'<link rel="alternate" hreflang="{other_lang}" href="{other_href}">\n'
            f"<style>{SITE_CSS}</style>\n</head>\n<body>\n<main>\n"
            + "\n".join(x for x in parts if x) + "\n</main>\n</body>\n</html>\n")


def find_browser() -> str | None:
    env = os.environ.get("LOGBOOK_BROWSER")
    if env:
        return env
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "msedge"):
        if shutil.which(name):
            return shutil.which(name)
    return None


def print_pdf(html_path: Path, pdf_path: Path, browser: str) -> None:
    profile_dir = tempfile.mkdtemp(prefix="logbook-chrome-")
    try:
        subprocess.run([browser, "--headless", "--disable-gpu", "--no-first-run",
                        f"--user-data-dir={profile_dir}", "--no-pdf-header-footer",
                        "--print-to-pdf-no-header", f"--print-to-pdf={pdf_path}", html_path.as_uri()],
                       check=True, capture_output=True, timeout=180)
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)
    if not pdf_path.exists():
        raise SystemExit(f"{browser} ran but wrote no PDF")


def load_views() -> list[tuple[Path, dict]]:
    views = []
    for d in VIEW_DIRS:
        for p in sorted(d.glob("*.toml")):
            v = load_toml(p)
            if v.get("lang") not in LANGS:
                raise SystemExit(f"{rel(p)}: `lang` must be one of {', '.join(LANGS)}")
            views.append((p, v))
    return views


# ---- commands -----------------------------------------------------------------------------------

def gather():
    profile = load_toml(PROFILE)
    skills = load_toml(SKILLS)
    for slug, s in skills.items():
        if not SLUG.match(slug) or not all(isinstance(s.get(k), str) for k in LANGS):
            raise SystemExit(f"skills.toml: [{slug}] needs a lowercase-hyphen slug and both `en` and `tr`")
    return profile, skills, load_entries()


def report(errors, warnings, notes) -> None:
    for n in notes:
        print(f"  note  {n}")
    for w in warnings:
        print(f"  warn  {w}")
    for e in errors:
        print(f"  ERROR {e}")


def cmd_check(args) -> int:
    profile, skills, entries = gather()
    errors, warnings, notes = validate(entries, skills, dt.date.today())
    errors += privacy_errors(load_private())
    for path, want in public_outputs(entries, skills, profile).items():
        have = path.read_text(encoding="utf-8") if path.exists() else None
        if have is None or have.replace("\r\n", "\n") != want:
            errors.append(f"{rel(path)} is out of date; run `python tools/logbook.py build`")
    report(errors, warnings, notes)
    by = {s: sum(e.status == s for e in entries) for s in STATUSES}
    print(f"{len(entries)} entries: {by['approved']} approved, {by['draft']} draft, {by['retired']} retired. "
          f"{len(errors)} error(s), {len(warnings)} warning(s).")
    drafts = [e for e in entries if e.status == "draft"]
    if drafts:
        print("awaiting sign-off: " + ", ".join(f"{e.num} {e.t('title', 'en') or e.slug}" for e in drafts))
    return 1 if errors else 0


def write(path: Path, content: str) -> bool:
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old is not None and old.replace("\r\n", "\n") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    return True


def cmd_build(args) -> int:
    profile, skills, entries = gather()
    errors, warnings, notes = validate(entries, skills, dt.date.today())
    private = load_private()
    errors += privacy_errors(private)
    if errors:
        report(errors, warnings, notes)
        print("not building: fix the errors above first")
        return 1
    for path, content in public_outputs(entries, skills, profile).items():
        print(f"  {'wrote' if write(path, content) else 'same '} {rel(path)}")
    today = dt.date.today()
    if not private.get("name"):
        print("  note  private/profile.toml has no `name`; the CVs use the public display name")
    want_pdf = args.pdf or args.pack
    browser = find_browser() if want_pdf else None
    if want_pdf and not browser:
        print("  note  no Chrome/Edge/Chromium found for --pdf; set LOGBOOK_BROWSER to its path")
    for path, view in load_views():
        stem = path.stem
        html_path = OUT / f"{stem}.html"
        write(html_path, render_cv(view, entries, skills, profile, private, today))
        write(OUT / f"{stem}.txt", render_forms(view, entries, profile))
        line = f"  wrote out/{stem}.html, out/{stem}.txt"
        if browser:
            print_pdf(html_path, OUT / f"{stem}.pdf", browser)
            line += f", out/{stem}.pdf"
            if args.pack:
                line += f", {make_pack(stem, view, entries)}"
        print(line)
    report([], warnings, notes)
    return 0


SKELETON = """# Logbook entry {num}. Rules: AGENTS.md. Stays a draft until the owner signs it off.
kind = "{kind}"
status = "draft"
date = "{date}"
skills = []

[title]
en = ""
tr = ""

[summary]
en = ""
tr = ""

[[evidence]]
level = "self"   # verifiable | artifact | document | self, strongest first
label = ""
# url = "https://..."              for verifiable and artifact
# file = "private/evidence/..."    for document, with sha256 from `python tools/logbook.py hash`
# sha256 = ""
"""


def cmd_new(args) -> int:
    if args.kind not in KINDS:
        raise SystemExit(f"kind must be one of {', '.join(KINDS)}")
    if not SLUG.match(args.slug):
        raise SystemExit("slug is lowercase words joined by hyphens, e.g. aws-cloud-practitioner")
    date = args.date or dt.date.today().isoformat()
    errors: list[str] = []
    check_date("--date", "date", date, dt.date.today(), errors)
    if errors:
        raise SystemExit(errors[0])
    entries = load_entries()
    num = f"{max((int(e.num) for e in entries), default=0) + 1:04d}"
    path = ENTRY_DIR / date[:4] / f"{num}-{args.slug}.toml"
    write(path, SKELETON.format(num=num, kind=args.kind, date=date))
    print(rel(path))
    return 0


def cmd_hash(args) -> int:
    for f in args.files:
        p = Path(f)
        print(f"{sha256(p)}  {rel(p)}")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Check and build the logbook.")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("check", help="validate entries and fail if generated files are stale")
    b = sub.add_parser("build", help="regenerate README, header SVGs and out/ CVs")
    b.add_argument("--pdf", action="store_true", help="also print each CV to PDF with Chrome/Edge")
    b.add_argument("--pack", action="store_true",
                   help="--pdf, plus a folder per CV holding the PDF and the documents behind its entries")
    n = sub.add_parser("new", help="start a draft entry with the next number")
    n.add_argument("kind")
    n.add_argument("slug")
    n.add_argument("--date", help="when it happened: YYYY, YYYY-MM or YYYY-MM-DD (default today)")
    h = sub.add_parser("hash", help="SHA-256 of evidence files")
    h.add_argument("files", nargs="+")
    args = ap.parse_args()
    return {"check": cmd_check, "build": cmd_build, "new": cmd_new, "hash": cmd_hash,
            None: cmd_check}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
