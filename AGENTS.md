# AGENTS.md

Instructions for any coding agent working in this repository (Claude Code, Codex, Gemini CLI,
Copilot, Cursor, or the next one), and for the owner. `CLAUDE.md` and `GEMINI.md` only point here.

## What this repository is

A logbook: a numbered, append-only record of the owner's projects, awards, courses, certificates,
education and experience, each entry carrying its evidence. CVs, the README and plain-text answers
for application forms are **compiled** from the entries by `tools/logbook.py`. Nobody edits a CV
by hand, and no agent writes a claim that an entry does not already back.

The owner is a software engineering student. They will ask you to log new things, process the
inbox, tailor a CV for an application, or tidy entries. They write to you in Turkish or English.

## The five rules

1. **Never invent a fact.** Dates, grades, scores, ranks, titles, organisations, numbers and
   outcomes come from a document, a link or the owner's own words in this conversation. When you
   do not know something, leave it out and say so in `notes`. A smaller true entry beats a fuller
   guessed one.
2. **Everything you write is a draft.** New entries get `status = "draft"`. You set
   `status = "approved"` only when the owner, in the current conversation, tells you to sign off
   that specific entry. Drafts never appear in the README or in a CV.
3. **Every entry carries evidence**, at the strongest level that exists (see below). If there is
   none, the entry says so with `level = "self"`.
4. **Private stays private.** The owner's real name, email, phone, address, birth date, ID numbers,
   grades they have not chosen to publish, and the places they are applying to never go into a
   tracked file. They live in `private/` (gitignored). `check` fails if a value from
   `private/profile.toml` appears in a tracked file, and it never prints the value.
5. **Append, do not rewrite history.** Entry numbers are never reused or renumbered. To correct an
   approved entry's facts, ask the owner; if the change is more than a typo, log a new entry with
   `supersedes = "NNNN"` and set the old one to `status = "retired"`.

## Before you commit

```bash
python tools/logbook.py build    # regenerates README.md and assets/header-*.svg
python tools/logbook.py check    # must print 0 error(s); CI runs the same command
```

Commit messages: `Log 0008: <title>` for a new entry, `Sign off 0003` when the owner approves one,
`Evidence for 0005: <what>` when adding evidence. One entry per commit when you can.

## Entry format

One file per entry: `entries/<year of date>/<NNNN>-<slug>.toml`. Start one with
`python tools/logbook.py new <kind> <slug> --date YYYY-MM-DD`, which takes the next number.

```toml
kind = "certificate"          # award | project | research | certificate | course | education
                              # | experience | activity | publication | talk
status = "draft"              # draft | approved | retired
date = "2027-01-15"           # when it happened (end date for spans): YYYY, YYYY-MM or YYYY-MM-DD
start = "2026-12-01"          # optional, for spans
# ongoing = true              # still going on (a degree, a job): needs `start`, has no `date`
# expected = "2028-06"        # ongoing only: expected end, the one date allowed in the future
skills = ["python"]           # slugs from skills.toml only; add a slug there first if needed
related = ["0001"]            # optional, other entry numbers
supersedes = "0004"           # optional
notes = """Not rendered anywhere. What is unconfirmed, where facts came from."""

[title]
en = "..."
tr = "..."

[org]                          # optional: issuer, school, company, event
en = "..."
tr = "..."

[role]                         # optional: "intern", "team lead", "solo entry"
en = "..."
tr = "..."

[summary]                      # one or two sentences; required in both languages to approve
en = "..."
tr = "..."

[lines]                        # optional CV lines; the CV shows these instead of the summary
en = ["...", "..."]
tr = ["...", "..."]

[[evidence]]                   # one or more
level = "verifiable"           # verifiable | artifact | document | self
label = "Coursera verification page"
url = "https://..."            # verifiable and artifact need an https URL
# file = "private/evidence/2027-01-15-aws-ccp.pdf"   document: path under private/ or evidence/
# sha256 = "..."                                     from `python tools/logbook.py hash <file>`
```

A field may be a single string when it is the same in both languages: `org = "GitHub Skills"`.

## Evidence levels, strongest first

| Level | Use it when | Examples |
|---|---|---|
| `verifiable` | a third party confirms the claim at a URL | Coursera `/verify/` link, Credly badge, Devpost project marked as a winner, official results page, university transcript verification |
| `artifact` | the work itself is public | repository, tagged release, live demo, published paper |
| `document` | a file proves it but should not be public | certificate PDF, award email, acceptance letter, internship letter |
| `self` | nothing else exists | a talk with no recording, a club role with no page |

A page the owner wrote about their own achievement (their README, their LinkedIn post) is not
evidence for it. For `document`, put the file in `private/evidence/` named
`YYYY-MM-DD-<slug>.<ext>` and record its hash; the file stays on the owner's machine and the hash
is public, so the owner can hand the file to an employer who can check it was not altered.

## Processing the inbox

When the owner says "process the inbox" / "inbox'ı işle":

1. Read each file in `inbox/` (certificate PDF or image, saved email, a list of links, notes).
2. Extract only what the file states. Note anything missing instead of filling it in.
3. Move documents to `private/evidence/YYYY-MM-DD-<slug>.<ext>` and hash them.
4. If the item has a public verification URL, add it as `verifiable` evidence. Prefer it to the
   document, but keep both.
5. Create one draft entry per achievement, in both languages, with skills from `skills.toml`.
6. Run `build` and `check`, commit, and tell the owner which drafts await their sign-off.
7. Delete nothing from `inbox/` that you did not move; tell the owner what you left and why.

## Education and other things still going on

A degree in progress is one entry: `kind = "education"`, `ongoing = true`, `start`, and
`expected` for the expected graduation. Title is the degree and department, `org` the university.
The best evidence a Turkish student usually has is an e-Devlet "Öğrenci Belgesi": keep the PDF as
`document` evidence in `private/evidence/`. Its barcode lets an employer check it on e-Devlet's
document verification page, so treat the barcode number as private too. Grades (GPA) go in `lines` only if the owner wants them
public; otherwise leave them out. On graduation, set `date`, remove `ongoing` and `expected`, and
add the diploma or graduation certificate as evidence.

## Making a CV for the owner

When the owner asks for a CV ("bana CV çıkar", "Türkçe özgeçmiş lazım"):

1. Check `private/profile.toml` exists and has `name` and `email`. If not, ask the owner to fill
   it in (template: `templates/private-profile.toml`). Never fill it in from guesses.
2. Run `python tools/logbook.py build --pdf`. The CVs are `out/cv-tr.pdf` and `out/cv-en.pdf`.
3. If the application also wants certificates, run `build --pack`: `out/cv-tr-pack/` then holds
   the CV and a copy of every private document behind the entries it shows, ready to attach.
4. Tell the owner which drafts are not on the CV because they are not signed off yet.

## Tailoring a CV for an application

1. Copy `views/cv-en.toml` (or `cv-tr.toml`) to `private/views/<company>-<role>.toml`. That folder
   is gitignored: where the owner applies is private.
2. Choose and order entries with `only`, `exclude`, `pin`, section `limit` and `min_level`.
   A view selects and orders entries; it never rewrites them.
3. If the posting needs a line no entry has, propose the line to the owner as an addition to that
   entry's `lines`. It is part of the record only once they agree.
4. `python tools/logbook.py build --pdf` writes `out/<view>.pdf` (and `.html`, `.txt`) using
   Chrome or Edge. `out/` is gitignored.

Turkish applications sometimes ask for fields an English CV never has. If the owner wants them,
they go in `private/profile.toml` under `[extra.tr]` and appear on the Turkish CV only.

## Writing

**English:** plain and specific. A verb, what was done, a number with its unit when there is one.
No "passionate", "leveraged", "cutting-edge", "spearheaded". State what was measured, not what was
hoped. Keep each CV line under 240 characters.

**Turkish:**
- No em dash (—). Use a full stop, a comma or a connective (`çünkü`, `böylece`, `ayrıca`).
- Keep technical terms in English when Turkish engineers say them in English: token, loss,
  pipeline, inference, commit, benchmark. Use the Turkish word only when it is the one people say:
  geliştirici, doğrulama, veritabanı.
- Suffixes on English terms and numbers follow pronunciation, after an apostrophe: `H100'de`,
  `GPT-2'yi`, `token'lık`, `tokenizer'ı`, `ARC-Easy'de`, `%94'ünü`. If you are unsure how a name
  is pronounced, rewrite the sentence so it takes no suffix.
- Decimal comma and thousands dot: `49,5 milyon`, `0,4217`, `7.000`.
- First-person past tense for what the owner did (`eğittim`, `kurdum`), not the passive.
- Vary sentence length; Turkish CV lines read better as two short sentences than one long one.
- No emoji, in either language.

## Commands

| Command | Does |
|---|---|
| `python tools/logbook.py check` | validate entries, evidence and hashes, guard privacy, and fail if README or header SVGs are stale |
| `python tools/logbook.py build [--pdf \| --pack]` | regenerate README, header SVGs and the site; write CVs and form text to `out/`; `--pack` adds a folder per CV with its documents |
| `python tools/logbook.py new KIND SLUG [--date D]` | start a draft entry with the next number |
| `python tools/logbook.py hash FILE...` | SHA-256 for `document` evidence |

Generated files: the part of `README.md` between the `logbook:begin` and `logbook:end` markers,
`assets/header-light.svg` / `assets/header-dark.svg`, and `docs/index.html` / `docs/tr/index.html`
(the public site, served by GitHub Pages). `docs/fonts/` holds the fonts every output uses, with their
OFL licenses. Edit the entries, never the generated files. Everything
in `README.md` outside the markers is hand-written and yours to edit.
