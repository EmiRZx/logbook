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
import base64
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

ENTRY_KEYS = {"kind", "status", "date", "start", "ongoing", "expected", "title", "org", "role",
              "summary", "lines", "skills", "related", "supersedes", "notes", "evidence"}
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


# ---- shared rendering ---------------------------------------------------------------------------

def cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ").strip()


def fmt_key(k: tuple[int, int, int]) -> str:
    return f"{k[0]}" if not k[1] else f"{k[0]}-{k[1]:02d}"


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


def expected_note(e: Entry, lang: str) -> str:
    return f"{EXPECTED[lang]} {e.data['expected']}" if e.ongoing and e.data.get("expected") else ""


def skill_names(shown: list[Entry], skills: dict, lang: str) -> list[str]:
    """Skills in use across the given entries, most-evidenced first."""
    count: dict[str, int] = {}
    for e in shown:
        for s in e.data.get("skills", []):
            count[s] = count.get(s, 0) + 1
    return [skills[s][lang] for s in sorted(count, key=lambda s: (-count[s], skills[s][lang].casefold()))]


def approved(entries: list[Entry]) -> list[Entry]:
    return sorted((e for e in entries if e.status == "approved"), key=lambda e: e.sort_key, reverse=True)


# Fog-grey stock, graphite ink, and one deep sage that only marks things. Colour proper is kept
# for the name, and only while the pointer is on it.
INK = {
    "light": {"paper": "#f3f4f1", "ink": "#1e242c", "accent": "#2e6b5e", "wash": "#e2e8e3",
              "muted": "#68707b", "rule": "#d9dcd6"},
    "dark": {"paper": "#121518", "ink": "#e6e8e5", "accent": "#7cc2ae", "wash": "#1f2b28",
             "muted": "#9aa1a8", "rule": "#2a2f34"},
}

# Fonts live in docs/fonts so GitHub Pages serves them to the site; the CVs and the README header
# embed the same files, because a PDF and an SVG on GitHub cannot fetch anything.
FONT_DIR = DOCS / "fonts"
UNICODE_RANGE = {
    "latin": ("U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, "
              "U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD"),
    "latin-ext": ("U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, "
                  "U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, "
                  "U+2113, U+2C60-2C7F, U+A720-A7FF"),
}
FACES = [  # family, file, weight, style, stretch
    ("Anybody", "anybody-{s}.woff2", "100 900", "normal", "50% 150%"),
    ("Familjen Grotesk", "familjen-grotesk-{s}-400-normal.woff2", "400", "normal", None),
    ("Familjen Grotesk", "familjen-grotesk-{s}-600-normal.woff2", "600", "normal", None),
    ("Familjen Grotesk", "familjen-grotesk-{s}-400-italic.woff2", "400", "italic", None),
    ("Martian Mono", "martian-mono-{s}-400-normal.woff2", "400", "normal", None),
]


def font_faces(base: str | None, subsets=("latin", "latin-ext"), files=None) -> str:
    """@font-face rules. `base` is a URL prefix for files served beside the page; None embeds each
    file as a data URI, for a document that has to stand alone."""
    rules = []
    for family, pattern, weight, style, stretch in FACES:
        if files and pattern not in files:
            continue
        for sub in subsets:
            path = FONT_DIR / pattern.format(s=sub)
            if base is None:
                if not path.exists():
                    raise SystemExit(f"missing font {rel(path)}")
                src = "data:font/woff2;base64," + base64.b64encode(path.read_bytes()).decode()
            else:
                src = base + path.name
            rules.append(f"@font-face{{font-family:'{family}';src:url({src}) format('woff2');"
                         f"font-weight:{weight};font-style:{style};"
                         + (f"font-stretch:{stretch};" if stretch else "")
                         + f"font-display:swap;unicode-range:{UNICODE_RANGE[sub]}}}")
    return "\n".join(rules)


def glyph_svg(level: str) -> str:
    inner = {
        "verifiable": '<circle cx="5" cy="5" r="4.2" fill="currentColor"/>',
        "artifact": '<polygon points="5,0.4 9.6,5 5,9.6 0.4,5" fill="currentColor"/>',
        "document": ('<rect x="1" y="1" width="8" height="8" fill="none" stroke="currentColor" stroke-width="1.1"/>'
                     '<rect x="1" y="1" width="4" height="8" fill="currentColor"/>'),
        "self": '<circle cx="5" cy="5" r="3.8" fill="none" stroke="currentColor" stroke-width="1.1"/>',
    }[level]
    return f'<svg class="g" viewBox="0 0 10 10" aria-hidden="true">{inner}</svg>'


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


def grain(color: str, alpha: str) -> str:
    """Paper grain as an SVG turbulence filter, in one ink colour. `alpha` is the matrix row that
    turns noise into sparse specks."""
    r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return (f"<filter id='n' x='0' y='0' width='100%' height='100%'>"
            f"<feTurbulence type='fractalNoise' baseFrequency='.82' numOctaves='3' stitchTiles='stitch'/>"
            f"<feColorMatrix values='0 0 0 0 {r:.3f} 0 0 0 0 {g:.3f} 0 0 0 0 {b:.3f} {alpha}'/></filter>")


# ---- the README block and the header SVG --------------------------------------------------------

def render_block(entries: list[Entry], skills: dict, profile: dict) -> str:
    shown = approved(entries)
    out = [
        "<picture>",
        '  <source media="(prefers-color-scheme: dark)" srcset="assets/header-dark.svg">',
        f'  <img src="assets/header-light.svg" width="880" alt="{html.escape(svg_alt(shown, profile))}">',
        "</picture>",
        "",
        "| No. | When | Entry | Evidence |",
        "|---|---|---|---|",
    ]
    for e in shown:
        sub = " · ".join(x for x in (e.t("org", "en"), expected_note(e, "en")) if x)
        entry = f"**[{cell(e.t('title', 'en'))}]({e.rel})**" + (f"<br><sub>{cell(sub)}</sub>" if sub else "")
        ev = []
        for x in e.evidence:
            label = cell(text(x.get("label"), "en"))
            ev.append(f"{MARK[x['level']]} [{label}]({x['url']})" if x.get("url") else f"{MARK[x['level']]} {label}")
        out.append(f"| `{e.num}` | {when_short(e)} | {entry} | {'<br>'.join(ev)} |")
    names = skill_names(shown, skills, "en")
    if names:
        out += ["", "**Skills** · " + " · ".join(names)]
    return "\n".join(out)


def svg_alt(shown: list[Entry], profile: dict) -> str:
    name = profile.get("display_name", "")
    if not shown:
        return f"logbook of {name}: no entries yet"
    keys = [e.date_key for e in shown]
    return f"logbook of {name}: {len(shown)} entries, {fmt_key(min(keys))} to {fmt_key(max(keys))}"


def render_svg(entries: list[Entry], profile: dict, theme: str) -> str:
    c = INK[theme]
    shown = sorted(approved(entries), key=lambda e: e.sort_key)
    name = html.escape(profile.get("display_name", ""))
    headline = html.escape(text(profile.get("headline"), "en"))
    keys = [e.date_key for e in shown]
    period = f"{fmt_key(min(keys))} → {fmt_key(max(keys))}" if keys else "none yet"
    W, H = 880, 184
    speck = c["ink"] if theme == "light" else "#ffffff"
    alpha = "0 0 0 1.25 -0.64" if theme == "light" else "0 0 0 1.1 -0.66"
    faces = font_faces(None, subsets=("latin",), files={"anybody-{s}.woff2", "martian-mono-{s}-400-normal.woff2",
                                                        "familjen-grotesk-{s}-400-italic.woff2"})
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
        f'role="img" aria-label="{html.escape(svg_alt(shown, profile))}">',
        f"<defs><style>{faces}"
        ".w{font-family:Anybody;font-variation-settings:'wdth' 125,'wght' 760}"
        ".m{font-family:'Martian Mono',monospace}.b{font-family:'Familjen Grotesk',sans-serif}"
        f"</style>{grain(speck, alpha)}<clipPath id='c'><rect width='{W}' height='{H}' rx='10'/></clipPath></defs>",
        f'<g clip-path="url(#c)"><rect width="{W}" height="{H}" fill="{c["paper"]}"/>',
        f'<rect width="{W}" height="{H}" filter="url(#n)" opacity="{0.4 if theme == "light" else 0.22}"/></g>',
        f'<text class="m" x="36" y="42" font-size="11" fill="{c["accent"]}">logbook</text>',
        f'<text class="w" x="34" y="94" font-size="48" fill="{c["ink"]}">{name}</text>',
        f'<text class="b" x="36" y="122" font-size="15" font-style="italic" fill="{c["muted"]}">{headline}</text>',
    ]
    for i, (label, value) in enumerate([("entries", str(len(shown))), ("period", period)]):
        parts.append(f'<text class="m" x="844" y="{42 + i * 24}" font-size="11" text-anchor="end" fill="{c["ink"]}">'
                     f'<tspan fill="{c["muted"]}">{label}  </tspan>{html.escape(value)}</text>')
    # One stamp per approved entry, oldest first, under the mark of its evidence.
    x = 36
    for e in shown[-11:]:
        parts.append(glyph(e.level or "self", x + 21, 142, 4, c["accent"], c["paper"]))
        parts.append(f'<rect x="{x}" y="153" width="42" height="17" rx="2" fill="{c["wash"]}"/>')
        parts.append(f'<text class="m" x="{x + 21}" y="165.5" font-size="9.5" text-anchor="middle" fill="{c["ink"]}">{e.num}</text>')
        x += 48
    lx = 844
    for lv in reversed(LEVELS):
        label = LEVEL_NAME["en"][lv]
        parts.append(f'<text class="m" x="{lx}" y="165.5" font-size="9.5" text-anchor="end" fill="{c["muted"]}">{label}</text>')
        lx -= len(label) * 9.5 * 0.74 + 9   # Martian Mono advances ~0.74em
        parts.append(glyph(lv, lx, 162, 3.8, c["accent"], c["paper"]))
        lx -= 22
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
    "en": {"kicker": "logbook extract", "provenance": "Evidence for each numbered entry:",
           "skills": "Skills", "documents": "Documents"},
    "tr": {"kicker": "defterden döküm", "provenance": "Numaralı her kaydın kanıtı:",
           "skills": "Yetenekler", "documents": "Belgeler"},
}

CV_CSS = """
@page { size: A4; margin: 12mm 15mm 11mm; }
:root { --ink:#1e242c; --accent:#2e6b5e; --wash:#e4ebe6; --muted:#616975; --rule:#d3d7d1;
        --body:'Familjen Grotesk',sans-serif; --mono:'Martian Mono',monospace; }
* { box-sizing: border-box; }
html { font-size: 9.1pt; }
body { margin: 0; color: var(--ink); background: #fff; font-family: var(--body); line-height: 1.36;
       -webkit-print-color-adjust: exact; print-color-adjust: exact; }
a { color: inherit; text-decoration: none; }
.kicker { font-family: var(--mono); font-size: 6.8pt; color: var(--muted); display: flex;
          justify-content: space-between; }
h1 { font-family: 'Anybody', sans-serif; font-variation-settings: 'wdth' 118, 'wght' 760;
     font-size: 21pt; line-height: 1.05; margin: 3mm 0 0; letter-spacing: -.01em; }
h1::after { content: ""; display: block; width: 24mm; height: 1.4mm; background: var(--accent); margin-top: 2mm; }
.headline { font-style: italic; color: var(--muted); margin: 2.2mm 0 0; font-size: 10.4pt; }
.contact { font-family: var(--mono); font-size: 7.2pt; margin: 2mm 0 0; }
.contact span + span::before, .extra span + span::before { content: "  /  "; color: var(--rule); white-space: pre; }
.extra { font-size: 8.4pt; color: var(--muted); margin: .8mm 0 0; }
.provenance { border-top: .8pt solid var(--ink); margin: 4mm 0 0; padding: 1.4mm 0 0; font-size: 7.6pt;
              color: var(--muted); display: flex; gap: 5mm; justify-content: space-between; }
.provenance .legend { font-family: var(--mono); font-size: 6.8pt; white-space: nowrap; }
.provenance .legend span { margin-left: 2.6mm; }
section { margin-top: 3.6mm; }
section.skills-sec { break-inside: avoid; }
h2 { font-family: var(--mono); font-size: 7.8pt; font-weight: 400; color: var(--accent); margin: 0 0 .8mm;
     display: flex; align-items: center; gap: 2mm; break-after: avoid; }
h2::before { content: ""; width: 2mm; height: 2mm; background: var(--accent); }
h2::after { content: ""; flex: 1; border-bottom: .5pt solid var(--rule); }
.entry { display: grid; grid-template-columns: 21mm 1fr 16mm; column-gap: 3mm; padding: .9mm 0;
         break-inside: avoid; }
.when { font-family: var(--mono); font-size: 6.8pt; color: var(--muted); padding-top: .7mm; white-space: nowrap; }
.title { font-weight: 600; font-size: 10.1pt; }
.org { color: var(--muted); }
.summary { margin: .3mm 0 0; }
ul { margin: .5mm 0 0; padding-left: 3.4mm; }
li { margin: .3mm 0; }
li::marker { content: "–  "; color: var(--muted); }
.ref { font-family: var(--mono); font-size: 6.8pt; text-align: right; padding-top: .6mm; white-space: nowrap;
       color: var(--accent); }
.ref a { color: var(--ink); background: var(--wash); padding: .2mm 1mm; }
svg.g { width: 6.8pt; height: 6.8pt; vertical-align: -.6pt; margin-right: 1.2mm; }
.skills { margin: 0; }
.appendix { break-before: page; margin-top: 0; }
/* The hash line is not decoration: a page holding a large image and one short caption is read
   by some PDF text extractors (PDFium among them) as vertical text, one letter per line. A
   second full-width line settles the direction, and it ties the image to the entry's hash. */
.doc { margin: 0 0 8mm; break-inside: avoid; }
.cap { font-size: 8.4pt; color: var(--muted); margin: 0 0 2mm; }
.cap b { font-family: var(--mono); font-weight: 400; font-size: 7pt; color: var(--ink);
               background: var(--wash); padding: .3mm 1.2mm; margin-right: 2mm; }
.doc img { display: block; max-width: 100%; max-height: 120mm; border: .5pt solid var(--rule); }
.hash { font-family: var(--mono); font-size: 6.6pt; color: var(--muted); margin: 1.5mm 0 0; }
@media screen {
  body { background: #e4e3de; }
  main { background: #fff; width: 210mm; min-height: 297mm; margin: 8mm auto; padding: 12mm 15mm 11mm;
         box-shadow: 0 1px 4px rgba(0,0,0,.18); }
}
"""


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
    used: list[Entry] = []
    for sec, picked in cv_sections(view, entries):
        used += picked
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
                f'<div class="ref">{glyph_svg(e.level)}<a href="{esc(entry_url(profile, e))}">{e.num}</a></div>'
                f"</article>")
        body.append("</section>")
    names = skill_names(used, skills, lang)
    if view.get("show_skills", True) and names:
        body.append(f"<section class=\"skills-sec\"><h2>{esc(ui['skills'])}</h2><p class=\"skills\">{esc(' · '.join(names))}</p></section>")
    # Certificates and letters behind the entries above, one per figure, after the CV proper.
    figures = []
    for e in used:
        for ev in e.evidence:
            img = preview_for(ev)
            if img and view.get("appendix", True):
                label = text(ev.get("label"), lang) or text(ev.get("label"), "en")
                data = base64.b64encode(img.read_bytes()).decode()
                figures.append(f'<div class="doc"><p class="cap"><b>{e.num}</b> {esc(label)} · {esc(e.t("title", lang))}'
                               f'</p><img src="data:{IMAGE_TYPES[img.suffix.lower()]};base64,{data}" '
                               f'alt="{esc(label)}"><p class="hash">SHA-256 {esc(ev.get("sha256", ""))}</p></div>')
    if figures:
        body.append(f'<section class="appendix"><h2>{esc(ui["documents"])}</h2>{"".join(figures)}</section>')
    title = f"{name} · {text(view.get('title'), lang) or 'CV'}"
    return (f'<!doctype html>\n<html lang="{lang}">\n<head>\n<meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f"<title>{esc(title)}</title>\n<style>{font_faces(None)}\n{CV_CSS}</style>\n</head>\n<body>\n<main>\n"
            + "\n".join(x for x in body if x) + "\n</main>\n</body>\n</html>\n")


IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def preview_for(ev: dict) -> Path | None:
    """The image a CV can show for a document: the file itself if it is an image, otherwise the
    `<name>.preview.jpg` that the `preview` command renders beside a PDF."""
    if ev.get("level") != "document" or not ev.get("file"):
        return None
    p = ROOT / ev["file"]
    if p.suffix.lower() in IMAGE_TYPES and p.is_file():
        return p
    for ext in (".jpg", ".png"):
        q = p.with_name(p.stem + ".preview" + ext)
        if q.is_file():
            return q
    return None


def render_preview(pdf: Path, width: int = 1600) -> Path:
    """First page of a PDF as a JPEG beside it. Uses pypdfium2 + Pillow if importable, otherwise
    poppler's pdftoppm. The only optional dependency in this tool, and only for this command."""
    out = pdf.with_name(pdf.stem + ".preview.jpg")
    try:
        import pypdfium2 as pdfium
        page = pdfium.PdfDocument(str(pdf))[0]
        image = page.render(scale=width / page.get_width()).to_pil().convert("RGB")
        image.save(out, "JPEG", quality=82, optimize=True, progressive=True)
        return out
    except ImportError:
        pass
    exe = shutil.which("pdftoppm")
    if not exe:
        raise SystemExit("rendering a PDF preview needs `pip install pypdfium2 pillow` or poppler's pdftoppm")
    subprocess.run([exe, "-jpeg", "-jpegopt", "quality=82", "-singlefile", "-f", "1", "-l", "1",
                    "-scale-to-x", str(width), "-scale-to-y", "-1", str(pdf), str(out.with_suffix(""))],
                   check=True)
    return out


def render_forms(view: dict, entries: list[Entry], profile: dict) -> str:
    """Plain text for application forms that make you retype everything into boxes."""
    lang = view.get("lang", "en")
    blocks = []
    for e in ordered(view, select(view, entries)):
        org = e.t("org", lang)
        head = [e.t("title", lang), f"{when_short(e)}" + (f" · {org}" if org else "")]
        body = [e.t("summary", lang)] + [f"- {s}" for s in e.lines(lang)]
        tail = [f"Evidence: {x['url']}" for x in e.evidence if x.get("url")] + [f"Logbook: {entry_url(profile, e)}"]
        blocks.append("\n".join(head + body + tail))
    return "\n\n\n".join(blocks) + "\n"


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
    "en": {"other": ("tr", "türkçe", "tr/"), "fonts": "fonts/", "entries": "entries", "skills": "skills",
           "source": "source"},
    "tr": {"other": ("en", "english", "../"), "fonts": "../fonts/", "entries": "kayıt", "skills": "yetenekler",
           "source": "kaynak"},
}

SITE_CSS = """
:root { --paper:#f3f4f1; --ink:#1e242c; --accent:#2e6b5e; --wash:#e2e8e3; --muted:#68707b; --rule:#d9dcd6;
  --g1:#2e7d6b; --g2:#3d63b8; --g3:#8b5fbf;
  --body:'Familjen Grotesk',sans-serif; --mono:'Martian Mono',ui-monospace,monospace;
  --grain-blend: multiply; --grain-opacity: .4; color-scheme: light; }
@media (prefers-color-scheme: dark) {
  :root { --paper:#121518; --ink:#e6e8e5; --accent:#7cc2ae; --wash:#1f2b28; --muted:#9aa1a8; --rule:#2a2f34;
          --g1:#6fd0b8; --g2:#8fa8ff; --g3:#c39bff; --grain-blend: screen; --grain-opacity: .22;
          color-scheme: dark; } }
* { box-sizing: border-box; }
html { background: var(--paper); }
body { margin: 0; color: var(--ink); font-family: var(--body); font-size: 17px; line-height: 1.55; }
body::before { content: ""; position: fixed; inset: 0; pointer-events: none; z-index: 1;
  mix-blend-mode: var(--grain-blend); opacity: var(--grain-opacity); background-image: var(--grain); }
a { color: inherit; text-decoration: underline 1px var(--rule); text-underline-offset: 3px;
  transition: text-decoration-color .2s; }
a:hover { text-decoration-color: var(--accent); }
main { position: relative; z-index: 2; max-width: 820px; margin: 0 auto; padding: 44px 16px 72px; }
.top { display: flex; justify-content: space-between; font-family: var(--mono); font-size: 12px; color: var(--accent); }
.top a { text-decoration: none; color: var(--muted); }

/* The name: graphite at rest. Under the pointer a gradient fades in whose three stops keep trading
   places, so the colour drifts without ever restarting. */
@property --s1 { syntax: '<color>'; inherits: false; initial-value: #2e7d6b; }
@property --s2 { syntax: '<color>'; inherits: false; initial-value: #3d63b8; }
@property --s3 { syntax: '<color>'; inherits: false; initial-value: #8b5fbf; }
.name { position: relative; display: inline-block; margin: 26px 0 0; font-family: 'Anybody', sans-serif;
  font-variation-settings: 'wdth' 125, 'wght' 760; font-size: clamp(42px, 9vw, 64px); line-height: 1;
  letter-spacing: -.01em; cursor: default; }
.name::after { content: attr(data-name); position: absolute; inset: 0; color: transparent;
  background: linear-gradient(100deg, var(--s1), var(--s2) 50%, var(--s3));
  -webkit-background-clip: text; background-clip: text; opacity: 0; transition: opacity .5s ease;
  animation: drift 6s linear infinite; }
.name:hover::after { opacity: 1; }
@keyframes drift {
  0%, 100% { --s1: var(--g1); --s2: var(--g2); --s3: var(--g3); }
  33.333% { --s1: var(--g2); --s2: var(--g3); --s3: var(--g1); }
  66.667% { --s1: var(--g3); --s2: var(--g1); --s3: var(--g2); } }
@media (prefers-reduced-motion: reduce) { .name::after { animation: none; } }

.headline { color: var(--muted); margin: 10px 0 0; font-size: 18px; }
.links { font-family: var(--mono); font-size: 13px; margin: 10px 0 0; }
.ledger { display: flex; flex-wrap: wrap; gap: 8px; margin: 30px 0 0; padding: 0; list-style: none;
  font-family: var(--mono); font-size: 12px; }
.ledger li { border: 1px solid var(--rule); border-radius: 3px; padding: 4px 10px; color: var(--muted); }
.ledger b { color: var(--ink); font-weight: 400; }
svg.g { width: 10px; height: 10px; color: var(--accent); margin-right: 7px; vertical-align: -1px; }
section { margin-top: 44px; }
h2 { font-family: var(--mono); font-size: 13px; font-weight: 400; color: var(--accent); text-transform: lowercase;
  display: flex; align-items: center; gap: 10px; margin: 0 0 4px; }
h2::after { content: ""; flex: 1; border-bottom: 1px solid var(--rule); }
article { display: grid; grid-template-columns: 92px 1fr; gap: 0 22px; padding: 18px 0;
  border-bottom: 1px solid var(--rule); scroll-margin-top: 16px; }
article:last-child { border-bottom: 0; }
.stamp { font-family: var(--mono); font-size: 12px; color: var(--muted); }
.stamp a { display: inline-block; background: var(--wash); color: var(--ink); text-decoration: none;
  border-radius: 3px; padding: 2px 7px; font-size: 13px; }
.stamp span { display: block; margin-top: 8px; }
h3 { margin: 0; font-size: 21px; font-weight: 600; line-height: 1.25; }
.org { color: var(--muted); margin-top: 2px; }
.summary { margin: 8px 0 0; }
ul.lines { margin: 8px 0 0; padding-left: 18px; }
ul.lines li { margin: 4px 0; }
ul.lines li::marker { content: "–  "; color: var(--muted); }
ul.evidence { list-style: none; padding: 0; margin: 12px 0 0; font-family: var(--mono); font-size: 12.5px;
  color: var(--muted); }
ul.evidence li { margin: 4px 0; overflow-wrap: anywhere; }
.skills { margin: 8px 0 0; }
footer { margin-top: 56px; font-family: var(--mono); font-size: 12px; color: var(--muted); }
@media (max-width: 600px) {
  body { font-size: 16px; }
  article { grid-template-columns: 1fr; }
  .stamp { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
  .stamp span { margin: 0; }
}
"""


def site_sections(lang: str) -> list[dict]:
    """The site groups entries the way the public CV view of the same language does."""
    for p in sorted((ROOT / "views").glob("*.toml")):
        v = load_toml(p)
        if v.get("lang") == lang and v.get("section"):
            return v["section"]
    return [{"heading": k, "kinds": [k]} for k in KINDS]


def site_entry(e: Entry, lang: str, profile: dict) -> str:
    esc = html.escape
    bits = [x for x in (e.t("org", lang), e.t("role", lang), expected_note(e, lang)) if x]
    body = f'<p class="summary">{esc(e.t("summary", lang))}</p>'
    if e.lines(lang):
        body += '<ul class="lines">' + "".join(f"<li>{esc(x)}</li>" for x in e.lines(lang)) + "</ul>"
    items = []
    for x in e.evidence:
        label = esc(text(x.get("label"), lang) or text(x.get("label"), "en"))
        item = f'<a href="{esc(x["url"])}">{label}</a>' if x.get("url") else label
        items.append(f"<li>{glyph_svg(x['level'])}{item}</li>")
    return (f'<article id="e{e.num}"><div class="stamp"><a href="{esc(entry_url(profile, e))}" '
            f'title="{SITE_UI[lang]["source"]}">{e.num}</a><span>{esc(when_short(e))}</span></div>'
            f'<div><h3>{esc(e.t("title", lang))}</h3>'
            + (f'<div class="org">{esc(" · ".join(bits))}</div>' if bits else "")
            + body + f'<ul class="evidence">{"".join(items)}</ul></div></article>')


def site_grain() -> str:
    def one(color: str, alpha: str) -> str:
        svg = (f"<svg xmlns='http://www.w3.org/2000/svg' width='240' height='240'>{grain(color, alpha)}"
               f"<rect width='100%' height='100%' filter='url(%23n)'/></svg>")
        return f'url("data:image/svg+xml;utf8,{svg}")'
    return (f":root{{--grain:{one(INK['light']['ink'], '0 0 0 1.25 -0.6')}}}"
            f"@media (prefers-color-scheme: dark){{:root{{--grain:{one('#ffffff', '0 0 0 1.1 -0.62')}}}}}")


def render_site(lang: str, entries: list[Entry], skills: dict, profile: dict) -> str:
    """One public page per language, from approved entries only. Deterministic, so `check` can
    tell when it is stale: no build date, nothing from private/."""
    esc = html.escape
    ui = SITE_UI[lang]
    shown = approved(entries)
    name = profile.get("display_name", "")
    other_lang, other_label, other_href = ui["other"]
    links = profile.get("links", {})
    link_html = "  /  ".join(f'<a href="{esc(u)}">{esc(u.removeprefix("https://"))}</a>'
                             for u in (links.get("github"), links.get("linkedin"), links.get("website")) if u)
    ledger = [f"<li><b>{len(shown)}</b> {ui['entries']}</li>"]
    ledger += [f"<li>{glyph_svg(lv)}<b>{sum(e.level == lv for e in shown)}</b> {LEVEL_NAME[lang][lv]}</li>"
               for lv in LEVELS]
    parts = [
        f'<div class="top"><span>logbook</span>'
        f'<a href="{other_href}" hreflang="{other_lang}">{other_label}</a></div>',
        f'<h1 class="name" data-name="{esc(name)}">{esc(name)}</h1>',
        f'<p class="headline">{esc(text(profile.get("headline"), lang))}</p>',
        f'<p class="links">{link_html}</p>' if link_html else "",
        f'<ul class="ledger">{"".join(ledger)}</ul>',
    ]
    for sec in site_sections(lang):
        picked = [e for e in shown if e.kind in sec.get("kinds", [])]
        if picked:
            parts.append(f"<section><h2>{esc(text(sec.get('heading'), lang))}</h2>")
            parts += [site_entry(e, lang, profile) for e in picked]
            parts.append("</section>")
    names = skill_names(shown, skills, lang)
    if names:
        parts.append(f"<section><h2>{esc(ui['skills'])}</h2><p class=\"skills\">{esc(' · '.join(names))}</p></section>")
    repo = profile.get("repo", "")
    parts.append(f'<footer>{ui["source"]}: <a href="{esc(repo)}">{esc(repo.removeprefix("https://"))}</a></footer>')
    return (f'<!doctype html>\n<html lang="{lang}">\n<head>\n<meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f"<title>{esc(name)} · logbook</title>\n"
            f'<meta name="description" content="{esc(text(profile.get("headline"), lang))}">\n'
            f'<link rel="alternate" hreflang="{other_lang}" href="{other_href}">\n'
            f"<style>{font_faces(ui['fonts'])}\n{site_grain()}\n{SITE_CSS}</style>\n</head>\n<body>\n<main>\n"
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
    for f in sorted({ev["file"] for e in entries if e.status == "approved" for ev in e.evidence
                     if ev.get("level") == "document" and str(ev.get("file", "")).lower().endswith(".pdf")
                     and (ROOT / ev["file"]).is_file() and not preview_for(ev)}):
        print(f"  note  {f} has no preview, so CVs cannot show it: python tools/logbook.py preview {f}")
    report([], warnings, notes)
    return 0


def cmd_preview(args) -> int:
    for f in args.files:
        print(f"  wrote {rel(render_preview(Path(f).resolve()))}")
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
    pv = sub.add_parser("preview", help="render a PDF document's first page as an image the CV can show")
    pv.add_argument("files", nargs="+")
    h = sub.add_parser("hash", help="SHA-256 of evidence files")
    h.add_argument("files", nargs="+")
    args = ap.parse_args()
    return {"check": cmd_check, "build": cmd_build, "new": cmd_new, "hash": cmd_hash,
            "preview": cmd_preview, None: cmd_check}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
