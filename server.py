"""
Interview Practice MCP Server
------------------------------
A local MCP server that gives Claude persistent memory of your DSA / HLD /
LLD interview practice, backed by plain files on disk that you can read and
edit yourself.

Files (created automatically under INTERVIEW_PREP_DIR, default ~/interview-prep):
  revision.md          Human-readable chronological log. One section per session.
  index.json           Machine-readable index: sessions, weak areas, and the
                        per-problem practice tracker (what's been attempted,
                        when, how it went, and where its solution/doc lives
                        on disk -- doc_path is always an absolute path, so it
                        can point anywhere, including outside INTERVIEW_PREP_DIR).
  custom_catalog.json  Problems you've added yourself, merged with the
                        built-in catalog.json shipped next to this script.

Each of DSA / HLD / LLD has its own independently configurable root
directory -- DSA_SOLUTIONS_DIR, HLD_SOLUTIONS_DIR, LLD_SOLUTIONS_DIR (each
env var, defaulting to INTERVIEW_PREP_DIR/docs/<type> if unset) -- so they can
each point at an existing personal repo instead of living inside
INTERVIEW_PREP_DIR:
  HLD_SOLUTIONS_DIR/<id>.md       One markdown file per HLD problem.
  LLD_SOLUTIONS_DIR/<id>.md       One markdown file per LLD problem.
  DSA_SOLUTIONS_DIR/<topic>/<problem>.py
                                  One real .py file per DSA problem, grouped
                                  by topic folder, matching a typical
                                  personal LeetCode-practice layout (DSA is
                                  code, not markdown -- see save_dsa_solution).

scan_dsa_directory / import_solved_dsa_problem(s) backfill the tracker from
DSA files that already exist on disk; save_dsa_solution adds new ones in the
same convention (never overwriting without an explicit flag).

Design goal: Claude loads a COMPACT summary at session start
(get_progress_summary), not old transcripts or full docs. Full detail for a
specific session or problem is fetched on demand.

Typical flow:
  1. get_progress_summary()                     -- orient at the start
  2. suggest_next_problems("DSA")                -- or "HLD" / "LLD"
  3. ... work the problem with the user ...
  4. log_session(..., problem_id=...)             -- always, to update tracker
  5a. save_practice_doc(...)                      -- HLD/LLD: per-problem doc
  5b. save_dsa_solution(...)                      -- DSA: per-problem .py file

One-time / occasional housekeeping for DSA:
  scan_dsa_directory() -> match files to catalog ids yourself ->
  import_solved_dsa_problem(s)(...) to backfill the tracker without touching
  any files.
"""

import json
import os
import re
from datetime import date
from pathlib import Path
from typing import List

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Storage setup
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.environ.get("INTERVIEW_PREP_DIR", "~/interview-prep")).expanduser()
REVISION_MD = DATA_DIR / "revision.md"
INDEX_JSON = DATA_DIR / "index.json"
CUSTOM_CATALOG_JSON = DATA_DIR / "custom_catalog.json"
CATALOG_JSON = Path(__file__).resolve().parent / "catalog.json"

# Where each type's per-problem file lives. Each is independently overridable
# via env var so they can point at existing personal repos (e.g. an existing
# DSA_mock/, HLD/, LLD/ directory) instead of the INTERVIEW_PREP_DIR default.
DSA_SOLUTIONS_DIR = Path(os.environ.get("DSA_SOLUTIONS_DIR", DATA_DIR / "docs" / "dsa")).expanduser()
HLD_SOLUTIONS_DIR = Path(os.environ.get("HLD_SOLUTIONS_DIR", DATA_DIR / "docs" / "hld")).expanduser()
LLD_SOLUTIONS_DIR = Path(os.environ.get("LLD_SOLUTIONS_DIR", DATA_DIR / "docs" / "lld")).expanduser()
BEHAVIORAL_SOLUTIONS_DIR = Path(os.environ.get("BEHAVIORAL_SOLUTIONS_DIR", DATA_DIR / "docs" / "behavioral")).expanduser()
SOLUTION_ROOTS = {"DSA": DSA_SOLUTIONS_DIR, "HLD": HLD_SOLUTIONS_DIR, "LLD": LLD_SOLUTIONS_DIR, "Behavioral": BEHAVIORAL_SOLUTIONS_DIR}
CANDIDATE_CONTEXT_MD = BEHAVIORAL_SOLUTIONS_DIR / "candidate_context.md"
IGNORE_DIRS = {".idea", ".git", "__pycache__", ".pytest_cache", ".venv", "venv"}

# Maps this server's catalog topic tags to the topic-folder naming convention
# used by a typical by-topic DSA practice repo. Used when creating NEW files
# via save_dsa_solution. Unmapped/custom topics fall back to a naive
# Title_Case_With_Underscores conversion.
DSA_FOLDER_MAP = {
    "Arrays & Hashing": "Arrays_and_Hashing",
    "Two Pointers": "Two_Pointers",
    "Sliding Window": "Sliding_Window",
    "Stack": "Stack",
    "Binary Search": "Binary_Search",
    "Linked List": "Linked_List",
    "Trees": "Trees",
    "Tries": "Trie",
    "Heap / Priority Queue": "Heap",
    "Backtracking": "Backtracking",
    "Graphs": "Graphs",
    "Advanced Graphs": "Graphs",
    "1-D DP": "Dynamic_Programming",
    "2-D DP": "Dynamic_Programming",
    "Greedy": "Greedy",
    "Intervals": "Intervals",
    "Math & Geometry": "Math_and_Geometry",
    "Bit Manipulation": "Bit_Manipulation",
}

VALID_TYPES = {"HLD", "LLD", "DSA", "Behavioral", "Other"}
CATALOG_TYPES = {"DSA", "HLD", "LLD", "Behavioral"}
VALID_VERDICTS = {"Strong Hire", "Hire", "Lean Hire", "Lean No Hire", "No Hire"}
VALID_DIFFICULTIES = {"Easy", "Medium", "Hard"}
MIN_COMPETENCY_SCORE = 1
MAX_COMPETENCY_SCORE = 5

STALE_DAYS = int(os.environ.get("INTERVIEW_PREP_STALE_DAYS", "21"))

mcp = FastMCP("interview-memory")


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower())
    return slug.strip("-") or "problem"


def _normalize_problem_type(problem_type: str) -> str:
    """Case-insensitive match against CATALOG_TYPES, returning the canonical
    form (e.g. "dsa" -> "DSA", "behavioral" -> "Behavioral"), or "" if no
    entry in CATALOG_TYPES matches. CATALOG_TYPES mixes all-uppercase
    abbreviations (DSA/HLD/LLD) with title-cased names (Behavioral), so a
    blind .upper() would break matching for the latter."""
    key = problem_type.strip().lower()
    for t in CATALOG_TYPES:
        if t.lower() == key:
            return t
    return ""


def _dsa_folder_name(topic: str) -> str:
    if topic in DSA_FOLDER_MAP:
        return DSA_FOLDER_MAP[topic]
    words = re.findall(r"[A-Za-z0-9]+", topic)
    return "_".join(w.capitalize() for w in words) if words else "Misc"


def _empty_catalog() -> dict:
    return {"DSA": [], "HLD": [], "LLD": [], "Behavioral": []}


def _load_catalog() -> dict:
    """Bundled catalog.json (shipped with this server) merged with any
    custom problems the user has added via add_custom_problem."""
    bundled = _empty_catalog()
    if CATALOG_JSON.exists():
        bundled.update(json.loads(CATALOG_JSON.read_text(encoding="utf-8")))
    custom = _empty_catalog()
    if CUSTOM_CATALOG_JSON.exists():
        custom.update(json.loads(CUSTOM_CATALOG_JSON.read_text(encoding="utf-8")))
    return {t: list(bundled.get(t, [])) + list(custom.get(t, [])) for t in CATALOG_TYPES}


def _load_index() -> dict:
    if INDEX_JSON.exists():
        index = json.loads(INDEX_JSON.read_text(encoding="utf-8"))
    else:
        index = {}
    index.setdefault("sessions", [])
    index.setdefault("weak_areas", {})
    index.setdefault("competency_scores", {})
    problems = index.setdefault("problems", {})
    for t in CATALOG_TYPES:
        problems.setdefault(t, {})
    return index


def _save_index(index: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_JSON.write_text(json.dumps(index, indent=2), encoding="utf-8")


def _ensure_revision_md() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not REVISION_MD.exists():
        REVISION_MD.write_text(
            "# Interview Practice Revision Log\n\n"
            "Auto-maintained by the interview-memory MCP server. "
            "Newest sessions are appended at the bottom.\n",
            encoding="utf-8",
        )


def _days_since(iso_date: str) -> int:
    try:
        return (date.today() - date.fromisoformat(iso_date)).days
    except ValueError:
        return 0


def _doc_path(problem_type: str, slug: str) -> Path:
    """HLD/LLD docs are flat: HLD_SOLUTIONS_DIR/<id>.md, LLD_SOLUTIONS_DIR/<id>.md."""
    return SOLUTION_ROOTS[problem_type] / f"{slug}.md"


# ---------------------------------------------------------------------------
# Tools: session log + weak areas (general, all types)
# ---------------------------------------------------------------------------

@mcp.tool()
def get_progress_summary() -> str:
    """Compact summary of all practice so far: session table, current weak
    areas, and per-type catalog coverage (solved / due for revision). Call
    this at the START of every practice session to load memory without
    loading old transcripts or docs. Follow up with suggest_next_problems()
    for a specific pick."""
    index = _load_index()
    sessions = index["sessions"]
    lines = []

    if not sessions:
        lines.append("No sessions logged yet. This is the first session.")
    else:
        lines.append(f"{len(sessions)} session(s) practised so far.")
        lines.append("")
        lines.append("| # | Date | Type | Topic | Verdict |")
        lines.append("|---|------|------|-------|---------|")
        for s in sessions[-15:]:
            lines.append(
                f"| {s['id']} | {s['date']} | {s['type']} | {s['topic']} | {s['verdict']} |"
            )
        if len(sessions) > 15:
            lines.append(f"\n(showing last 15 of {len(sessions)}; use get_session_detail for older ones)")

    weak = index.get("weak_areas", {})
    if weak:
        lines += ["", "Current weak areas (area: times flagged):"]
        for area, count in sorted(weak.items(), key=lambda kv: -kv[1]):
            lines.append(f"- {area}: {count}")
        lines += [
            "",
            "Bias problem selection toward these weak areas, and avoid "
            "repeating recently practised problems unless revisiting a gap.",
        ]

    catalog = _load_catalog()
    lines += ["", "Catalog coverage:"]
    for t in ("DSA", "HLD", "LLD", "Behavioral"):
        total = len(catalog[t])
        tracker = index["problems"][t]
        attempted = len(tracker)
        due = sum(1 for info in tracker.values() if _days_since(info["last_practiced"]) >= STALE_DAYS)
        lines.append(f"- {t}: {attempted}/{total} catalog problems attempted, {due} due for revision")

    competency_scores = index.get("competency_scores", {})
    if competency_scores:
        lines += ["", "Competency scores (behavioral, 1-5 avg):"]
        for area, stats in sorted(competency_scores.items(), key=lambda kv: kv[1]["avg"]):
            lines.append(f"- {area}: {stats['avg']:.1f} ({stats['count']} rated)")

    return "\n".join(lines)


@mcp.tool()
def log_session(
    topic: str,
    interview_type: str,
    verdict: str,
    strengths: str,
    gaps: str,
    notes: str = "",
    problem_id: str = "",
    difficulty: str = "",
    competency_scores: dict = {},
    improvements: str = "",
) -> str:
    """Record a completed practice session. Call this at the END of every
    session, for every type (DSA, HLD, LLD, Behavioral, Other). Appends a
    human-readable entry to revision.md and updates the weak-area tracker.
    For DSA/HLD/LLD/Behavioral, also updates the per-problem tracker used by
    suggest_next_problems and get_progress_summary — pass problem_id (see
    get_catalog) when the problem is in the catalog so revision scheduling
    is accurate; otherwise one is derived from topic.

    Args:
        topic: What was practised, e.g. "Design a rate limiter" or "Two Sum".
        interview_type: One of HLD, LLD, DSA, Behavioral, Other.
        verdict: One of Strong Hire, Hire, Lean Hire, Lean No Hire, No Hire.
        strengths: Semicolon-separated list of what went well.
        gaps: Semicolon-separated list of weak areas / mistakes. These are
            aggregated across sessions, so reuse consistent short names
            (e.g. "capacity estimation", "consistent hashing").
        notes: Optional free-form notes, key takeaways, follow-ups.
        problem_id: Optional catalog id (from get_catalog) for DSA/HLD/LLD/
            Behavioral, linking this session to a specific tracked problem.
        difficulty: Optional Easy/Medium/Hard, used only if this problem
            isn't already in the catalog (ad-hoc problem).
        competency_scores: Optional dict of competency name -> integer rating
            1-5 (e.g. {"leadership": 4, "conflict resolution": 3}), mainly for
            Behavioral sessions. Tracked over time and averaged per
            competency in get_progress_summary.
        improvements: Optional free-form prose with concrete suggestions for
            improvement (distinct from the short `gaps` tags), e.g. specific
            wording or structure to use next time on a STAR answer.
    """
    interview_type = interview_type.strip()
    if interview_type not in VALID_TYPES:
        return f"Invalid interview_type '{interview_type}'. Use one of: {sorted(VALID_TYPES)}"
    verdict = verdict.strip()
    if verdict not in VALID_VERDICTS:
        return f"Invalid verdict '{verdict}'. Use one of: {sorted(VALID_VERDICTS)}"
    for area, score in competency_scores.items():
        if not isinstance(score, int) or not (MIN_COMPETENCY_SCORE <= score <= MAX_COMPETENCY_SCORE):
            return (
                f"Invalid score for competency '{area}': {score!r}. "
                f"Scores must be integers {MIN_COMPETENCY_SCORE}-{MAX_COMPETENCY_SCORE}."
            )

    index = _load_index()
    session_id = len(index["sessions"]) + 1
    today = date.today().isoformat()
    gap_list = [g.strip() for g in gaps.split(";") if g.strip()]
    strength_list = [s.strip() for s in strengths.split(";") if s.strip()]

    index["sessions"].append(
        {
            "id": session_id,
            "date": today,
            "type": interview_type,
            "topic": topic.strip(),
            "verdict": verdict,
            "gaps": gap_list,
        }
    )
    for g in gap_list:
        key = g.lower()
        index["weak_areas"][key] = index["weak_areas"].get(key, 0) + 1

    scores_tracker = index["competency_scores"]
    for area, score in competency_scores.items():
        key = area.strip().lower()
        stats = scores_tracker.setdefault(key, {"count": 0, "total": 0, "avg": 0.0, "history": []})
        stats["count"] += 1
        stats["total"] += score
        stats["avg"] = stats["total"] / stats["count"]
        stats["history"].append({"session_id": session_id, "date": today, "score": score})

    problem_note = ""
    if interview_type in CATALOG_TYPES:
        catalog_by_id = {c["id"]: c for c in _load_catalog()[interview_type]}
        slug = _slugify(problem_id) if problem_id.strip() else _slugify(topic)
        catalog_entry = catalog_by_id.get(slug)
        tracker = index["problems"][interview_type]
        existing = tracker.get(slug, {})
        entry = {
            "title": catalog_entry["title"] if catalog_entry else topic.strip(),
            "topic": catalog_entry["topic"] if catalog_entry else "",
            "difficulty": (catalog_entry["difficulty"] if catalog_entry
                            else (difficulty if difficulty in VALID_DIFFICULTIES else existing.get("difficulty", ""))),
            "times_practiced": existing.get("times_practiced", 0) + 1,
            "last_practiced": today,
            "last_verdict": verdict,
            "history": existing.get("history", []) + [{"date": today, "verdict": verdict}],
            "doc_path": existing.get("doc_path"),
        }
        tracker[slug] = entry
        problem_note = f" Problem tracker updated for {interview_type} `{slug}` (attempt #{entry['times_practiced']})."

    _save_index(index)

    _ensure_revision_md()
    strength_lines = [f"- {s}" for s in strength_list] or ["- (none recorded)"]
    gap_lines = [f"- {g}" for g in gap_list] or ["- (none recorded)"]
    entry_lines = [
        f"\n---\n\n## Session {session_id} — {today} — [{interview_type}] {topic.strip()}",
        f"\n**Verdict:** {verdict}\n",
        "**Went well:**",
        *strength_lines,
        "\n**Gaps / to revise:**",
        *gap_lines,
    ]
    if competency_scores:
        entry_lines += ["\n**Competency scores:**"]
        entry_lines += [f"- {area}: {score}/5" for area, score in competency_scores.items()]
    if improvements.strip():
        entry_lines += ["\n**Suggestions for improvement:**", improvements.strip()]
    if notes.strip():
        entry_lines += ["\n**Notes:**", notes.strip()]
    with REVISION_MD.open("a", encoding="utf-8") as f:
        f.write("\n".join(entry_lines) + "\n")

    score_note = f" {len(competency_scores)} competency score(s) recorded." if competency_scores else ""
    return (
        f"Logged session {session_id} to {REVISION_MD}. "
        f"Weak-area tracker updated ({len(gap_list)} gap(s) recorded)."
        f"{problem_note}{score_note}"
    )


@mcp.tool()
def get_session_detail(session_id: int) -> str:
    """Fetch the full revision.md entry for one past session, by its id from
    the progress summary. Use when the user wants to revisit a specific
    session's feedback."""
    if not REVISION_MD.exists():
        return "No revision file exists yet."
    text = REVISION_MD.read_text(encoding="utf-8")
    marker = f"## Session {session_id} —"
    start = text.find(marker)
    if start == -1:
        return f"Session {session_id} not found in revision.md."
    end = text.find("\n---\n", start)
    return text[start:end if end != -1 else len(text)].strip()


@mcp.tool()
def resolve_weak_area(area: str) -> str:
    """Remove a weak area from the tracker once the user has demonstrably
    improved at it (e.g. handled it well in a later session)."""
    index = _load_index()
    key = area.strip().lower()
    if key in index["weak_areas"]:
        del index["weak_areas"][key]
        _save_index(index)
        return f"Removed '{key}' from weak areas. Nice progress."
    return f"'{key}' is not currently in the weak-area tracker."


# ---------------------------------------------------------------------------
# Tools: catalog + revision-aware suggestions
# ---------------------------------------------------------------------------

@mcp.tool()
def get_catalog(problem_type: str, topic: str = "", difficulty: str = "", status: str = "") -> str:
    """Browse the problem catalog for a practice type, annotated with your
    progress on each entry. Use this to see the full pool of problems (not
    just top suggestions) or to look up a problem's exact id before calling
    log_session or save_practice_doc with problem_id.

    Args:
        problem_type: One of DSA, HLD, LLD.
        topic: Optional substring filter on the topic tag (case-insensitive).
        difficulty: Optional exact filter: Easy, Medium, or Hard.
        status: Optional filter: "new" (never attempted), "attempted", or
            "due" (attempted but stale — due for revision).
    """
    problem_type = _normalize_problem_type(problem_type)
    if not problem_type:
        return f"problem_type must be one of {sorted(CATALOG_TYPES)}."

    entries = _load_catalog()[problem_type]
    if topic:
        entries = [e for e in entries if topic.lower() in e.get("topic", "").lower()]
    if difficulty:
        entries = [e for e in entries if e.get("difficulty", "").lower() == difficulty.lower()]

    index = _load_index()
    tracker = index["problems"][problem_type]

    rows = []
    for e in entries:
        info = tracker.get(e["id"])
        if info is None:
            row_status = "new"
            status_text = "New"
        else:
            days = _days_since(info["last_practiced"])
            if days >= STALE_DAYS:
                row_status = "due"
                status_text = f"Due ({days}d since last, {info['times_practiced']}x, last: {info['last_verdict']})"
            else:
                row_status = "attempted"
                status_text = f"Fresh ({days}d since last, {info['times_practiced']}x, last: {info['last_verdict']})"
        if status and status.lower() != row_status:
            continue
        rows.append((e, status_text))

    if not rows:
        return f"No {problem_type} catalog entries match the given filters."

    lines = [f"{problem_type} catalog ({len(rows)} shown of {len(entries)} filtered / {len(_load_catalog()[problem_type])} total):", ""]
    lines.append("| id | Title | Topic | Difficulty | Status |")
    lines.append("|---|---|---|---|---|")
    for e, status_text in rows:
        lines.append(f"| `{e['id']}` | {e['title']} | {e['topic']} | {e['difficulty']} | {status_text} |")
    return "\n".join(lines)


@mcp.tool()
def suggest_next_problems(problem_type: str, count: int = 5, topic: str = "") -> str:
    """Recommend which problems to tackle next for a given practice type, by
    scanning what's already been solved/attempted (from this MCP's memory)
    against the problem catalog. Call this whenever the user asks 'what
    should I practice next', 'quiz me', or wants a revision pick.

    Ranking: problems tied to a flagged weak area (whether new or due for
    revision) rank highest, then other due-for-revision problems (oldest
    attempt first), then other new problems. A problem attempted within the
    last {stale_days} days is not resurfaced unless it matches a weak area.

    Args:
        problem_type: One of DSA, HLD, LLD.
        count: How many suggestions to return (default 5).
        topic: Optional topic filter (substring match against the topic tag).
    """
    problem_type = _normalize_problem_type(problem_type)
    if not problem_type:
        return f"problem_type must be one of {sorted(CATALOG_TYPES)}."

    catalog = _load_catalog()[problem_type]
    if topic:
        catalog = [c for c in catalog if topic.lower() in c.get("topic", "").lower()]
    catalog_ids = {c["id"] for c in catalog}

    index = _load_index()
    tracker = index["problems"][problem_type]
    weak_areas = [k.lower() for k in index.get("weak_areas", {})]

    def weak_match(*texts: str) -> bool:
        for text in texts:
            text = (text or "").lower()
            if not text:
                continue
            if any(w in text or text in w for w in weak_areas):
                return True
        return False

    candidates = []

    for slug, info in tracker.items():
        if topic and slug not in catalog_ids:
            continue
        days = _days_since(info["last_practiced"])
        if days < STALE_DAYS and not weak_match(info.get("topic", ""), info.get("title", "")):
            continue
        score = 50 + min(days, 365) / 365 * 10
        if weak_match(info.get("topic", ""), info.get("title", "")):
            score += 100
        candidates.append({
            "id": slug,
            "title": info.get("title", slug),
            "topic": info.get("topic", ""),
            "difficulty": info.get("difficulty", ""),
            "status": "due_for_revision",
            "reason": f"last attempted {days}d ago (verdict: {info.get('last_verdict', 'n/a')})",
            "score": score,
        })

    for c in catalog:
        if c["id"] in tracker:
            continue
        score = {"Easy": 3, "Medium": 2, "Hard": 1}.get(c.get("difficulty", ""), 0)
        matched = weak_match(c.get("topic", ""))
        if matched:
            score += 100
        candidates.append({
            "id": c["id"],
            "title": c["title"],
            "topic": c.get("topic", ""),
            "difficulty": c.get("difficulty", ""),
            "status": "new",
            "reason": "not yet attempted" + (" — matches a weak area" if matched else ""),
            "score": score,
        })

    if not candidates:
        return (
            f"No candidates found for {problem_type}"
            + (f" (topic filter: {topic})" if topic else "")
            + ". Either everything is fresh, or the catalog/filter is empty — "
              "try widening the topic filter or add_custom_problem."
        )

    candidates.sort(key=lambda c: -c["score"])
    top = candidates[:count]

    lines = [f"Top {len(top)} {problem_type} suggestion(s):", ""]
    lines.append("| id | Title | Topic | Difficulty | Status | Why |")
    lines.append("|---|---|---|---|---|---|")
    for c in top:
        lines.append(f"| `{c['id']}` | {c['title']} | {c['topic']} | {c['difficulty']} | {c['status']} | {c['reason']} |")
    return "\n".join(lines)


@mcp.tool()
def add_custom_problem(problem_type: str, title: str, topic: str = "General", difficulty: str = "Medium") -> str:
    """Add a problem to the personal catalog that isn't already in the
    built-in list (e.g. a problem from a mock interview, a company-specific
    question). Once added it participates in get_catalog and
    suggest_next_problems like any built-in entry.

    Args:
        problem_type: One of DSA, HLD, LLD.
        title: Problem title, e.g. "Design a Distributed Cron Scheduler".
        topic: Topic/category tag, used for weak-area matching.
        difficulty: Easy, Medium, or Hard.
    """
    problem_type = _normalize_problem_type(problem_type)
    if not problem_type:
        return f"problem_type must be one of {sorted(CATALOG_TYPES)}."
    if difficulty not in VALID_DIFFICULTIES:
        return f"difficulty must be one of {sorted(VALID_DIFFICULTIES)}."

    slug = _slugify(title)
    existing_ids = {c["id"] for c in _load_catalog()[problem_type]}
    if slug in existing_ids:
        return f"A problem with id `{slug}` already exists in the {problem_type} catalog."

    custom = _empty_catalog()
    if CUSTOM_CATALOG_JSON.exists():
        custom.update(json.loads(CUSTOM_CATALOG_JSON.read_text(encoding="utf-8")))
    custom[problem_type].append({"id": slug, "title": title.strip(), "topic": topic.strip(), "difficulty": difficulty})

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CUSTOM_CATALOG_JSON.write_text(json.dumps(custom, indent=2), encoding="utf-8")
    return f"Added `{slug}` ({title.strip()}) to the {problem_type} catalog."


# ---------------------------------------------------------------------------
# Tools: per-problem revision docs (HLD, LLD)
# ---------------------------------------------------------------------------

@mcp.tool()
def save_practice_doc(problem_type: str, title: str, content_markdown: str, problem_id: str = "") -> str:
    """Persist a full write-up of an HLD / LLD problem you just worked
    through with the user, as its OWN markdown file for quick future
    revision (one file per problem, e.g. docs/hld/design-rate-limiter.md).
    Call this at the end of a problem once you and the user have converged
    on a solution. For DSA, use save_dsa_solution instead (writes a .py file
    into your existing solutions repo, not a markdown doc here).

    Write the ENTIRE content yourself in content_markdown — this tool only
    persists it, it doesn't generate anything. Suggested shape per type:
      - HLD: requirements (functional/non-functional), capacity estimation,
        high-level architecture (components as text/ASCII diagram), API
        design, data model, deep dives on the hard parts, trade-offs, what
        you'd change under different constraints.
      - LLD: problem framing, key entities/classes with responsibilities,
        class diagram (as text), design patterns used and why, key design
        decisions and trade-offs, extensibility notes.

    Overwrites any existing doc for the same problem id. Also links the doc
    path into the problem tracker so list_practice_docs can find it.

    Args:
        problem_type: HLD or LLD.
        title: Problem title, e.g. "Design a Rate Limiter".
        content_markdown: The full write-up in markdown, authored by you.
        problem_id: Catalog id if this matches a cataloged problem (see
            get_catalog); otherwise leave blank and one is derived from title.
    """
    problem_type = problem_type.strip().upper()
    if problem_type not in {"HLD", "LLD"}:
        return "problem_type must be HLD or LLD (use save_dsa_solution for DSA)."

    # Always route through _slugify (never use problem_id/title raw) so a
    # value like "../../etc/passwd" can't escape docs/<type>/ via the
    # filename we build below -- _slugify strips everything but [a-z0-9-].
    slug = _slugify(problem_id) if problem_id.strip() else _slugify(title)

    catalog_entry = {c["id"]: c for c in _load_catalog()[problem_type]}.get(slug)
    index = _load_index()
    tracker = index["problems"][problem_type]
    existing = tracker.get(slug, {})
    topic = (catalog_entry["topic"] if catalog_entry else existing.get("topic", "")) or ""
    difficulty = (catalog_entry["difficulty"] if catalog_entry else existing.get("difficulty", "")) or ""

    path = _doc_path(problem_type, slug)
    allowed_root = SOLUTION_ROOTS[problem_type].resolve()
    if not path.resolve().is_relative_to(allowed_root):
        return f"Refusing to write outside {allowed_root}."
    path.parent.mkdir(parents=True, exist_ok=True)

    today = date.today().isoformat()
    header = f"# {title.strip()}\n\n_Last updated: {today} · Type: {problem_type} · id: `{slug}`_\n\n---\n\n"
    path.write_text(header + content_markdown.strip() + "\n", encoding="utf-8")

    abs_path = str(path.resolve())
    if slug in tracker:
        tracker[slug]["doc_path"] = abs_path
        tracker[slug]["topic"] = tracker[slug].get("topic") or topic
        tracker[slug]["difficulty"] = tracker[slug].get("difficulty") or difficulty
    else:
        tracker[slug] = {
            "title": title.strip(),
            "topic": topic,
            "difficulty": difficulty,
            "times_practiced": 0,
            "last_practiced": today,
            "last_verdict": "n/a",
            "history": [],
            "doc_path": abs_path,
        }
    _save_index(index)

    return f"Saved {problem_type} doc for `{slug}` to {path}."


@mcp.tool()
def get_practice_doc(problem_type: str, problem_id: str) -> str:
    """Fetch a previously saved DSA/HLD/LLD practice doc in full, e.g. when
    the user wants to revise a specific past solution before a revisit
    session. Use list_practice_docs first if you don't know the exact id."""
    problem_type = _normalize_problem_type(problem_type)
    if not problem_type:
        return f"problem_type must be one of {sorted(CATALOG_TYPES)}."

    index = _load_index()
    info = index["problems"][problem_type].get(_slugify(problem_id))
    if not info or not info.get("doc_path"):
        return f"No saved doc for {problem_type} `{problem_id}`. Use list_practice_docs to see what exists."

    path = Path(info["doc_path"])
    # Guardrail: only ever read back from the directory this problem_type is
    # allowed to live in, even though doc_path is set by our own tools --
    # protects against a hand-edited/corrupted index.json pointing elsewhere.
    allowed_root = SOLUTION_ROOTS[problem_type].resolve()
    if not path.resolve().is_relative_to(allowed_root):
        return f"Refusing to read a doc_path outside {allowed_root} (index.json may be corrupted)."
    if not path.exists():
        return f"Doc was recorded at {path} but the file is missing (was it moved or deleted?)."
    return path.read_text(encoding="utf-8")


@mcp.tool()
def list_practice_docs(problem_type: str = "") -> str:
    """List all saved DSA/HLD/LLD practice docs (title, id, last updated) as
    a quick revision menu. Call at the start of a revision session when the
    user wants to pick from what's already documented, or to check whether a
    problem already has a doc before creating a duplicate. (Behavioral has no
    per-question docs — see save_candidate_context/get_candidate_context.)

    Args:
        problem_type: Optional filter: DSA, HLD, or LLD. Leave blank for all.
    """
    if problem_type.strip():
        normalized = _normalize_problem_type(problem_type)
        if not normalized:
            return f"problem_type must be one of {sorted(CATALOG_TYPES)}."
        types = [normalized]
    else:
        types = sorted(CATALOG_TYPES)

    index = _load_index()
    lines = []
    for t in types:
        tracker = index["problems"][t]
        docs = [(slug, info) for slug, info in tracker.items() if info.get("doc_path")]
        if not docs:
            continue
        lines.append(f"### {t} ({len(docs)} doc(s))")
        lines.append("| id | Title | Topic | Last updated | Path |")
        lines.append("|---|---|---|---|---|")
        for slug, info in sorted(docs, key=lambda kv: kv[1].get("last_practiced", ""), reverse=True):
            lines.append(f"| `{slug}` | {info['title']} | {info.get('topic', '')} | {info.get('last_practiced', '?')} | {info['doc_path']} |")
        lines.append("")

    if not lines:
        return "No practice docs saved yet. Use save_practice_doc after solving a problem."
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Tools: behavioral candidate context (single reusable profile + story bank)
# ---------------------------------------------------------------------------

@mcp.tool()
def save_candidate_context(content_markdown: str) -> str:
    """Persist your behavioral-interview candidate context as ONE reusable
    markdown file (BEHAVIORAL_SOLUTIONS_DIR/candidate_context.md), overwriting
    any previous version in full — same convention as save_practice_doc.
    Call this before a mock behavioral session (to set up your profile) and
    again whenever you're prepping for a new company/role and want to shift
    emphasis.

    Write the ENTIRE file yourself in content_markdown — this tool only
    persists it. Structure it in two clearly-marked sections so re-saves
    don't lose earlier work:
      - "## Core Profile & Story Bank" — stable across sessions: background
        summary, and your STAR stories tagged by competency (e.g. Leadership,
        Conflict & Disagreement, Failure & Growth). Carry this section
        forward unchanged unless a story genuinely improves.
      - "## Current Focus" — the company/role you're currently prepping for,
        and which stories/angles to emphasize for it. Replace this section
        each time you switch targets; the Core Profile section stays put.

    Args:
        content_markdown: The full candidate context file, authored by you.
    """
    path = CANDIDATE_CONTEXT_MD
    allowed_root = BEHAVIORAL_SOLUTIONS_DIR.resolve()
    if not path.resolve().is_relative_to(allowed_root):
        return f"Refusing to write outside {allowed_root}."
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content_markdown.strip() + "\n", encoding="utf-8")
    return f"Saved candidate context to {path}."


@mcp.tool()
def get_candidate_context() -> str:
    """Fetch the saved behavioral candidate context (background, STAR story
    bank, current focus) in full. Call this at the START of a behavioral
    mock session, before suggest_next_problems("Behavioral"), so stories can
    be tailored to what's already on file."""
    path = CANDIDATE_CONTEXT_MD
    allowed_root = BEHAVIORAL_SOLUTIONS_DIR.resolve()
    if not path.resolve().is_relative_to(allowed_root):
        return f"Refusing to read outside {allowed_root} (unexpected path)."
    if not path.exists():
        return "No candidate context saved yet. Use save_candidate_context to create one."
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Tools: DSA solutions directory (scan existing files, import, save new)
# ---------------------------------------------------------------------------

@mcp.tool()
def scan_dsa_directory(topic_folder: str = "") -> str:
    """Scan the on-disk DSA solutions directory (DSA_SOLUTIONS_DIR — e.g. an
    existing personal practice repo, organized as <topic-folder>/<file>.py)
    to see what's ACTUALLY been solved, independent of whether it was ever
    logged through this MCP. Returns each .py file's topic folder, filename,
    a short snippet from the top of the file (usually the problem statement
    docstring), and its last-modified date, plus whether it's already linked
    in the tracker.

    Use this for a one-time import: read the snippets, match each
    not-yet-imported file to a catalog id yourself (browse with get_catalog
    if needed — add_custom_problem first for anything not in the catalog),
    then call import_solved_dsa_problem or import_solved_dsa_problems.
    Re-run occasionally to catch solutions added outside a logged session.

    Args:
        topic_folder: Optional substring filter on the topic folder name
            (e.g. "Graphs", "Dynamic_Programming").
    """
    if not DSA_SOLUTIONS_DIR.exists():
        return (
            f"DSA_SOLUTIONS_DIR does not exist: {DSA_SOLUTIONS_DIR}. "
            "Set the DSA_SOLUTIONS_DIR env var in your MCP config to point "
            "at your existing solutions repo, or create the directory."
        )

    index = _load_index()
    tracked_paths = {
        str(Path(info["doc_path"]).resolve())
        for info in index["problems"]["DSA"].values()
        if info.get("doc_path")
    }

    rows = []
    for folder in sorted(p for p in DSA_SOLUTIONS_DIR.iterdir() if p.is_dir()):
        if folder.name in IGNORE_DIRS or folder.name.startswith("."):
            continue
        if topic_folder and topic_folder.lower() not in folder.name.lower():
            continue
        for f in sorted(folder.glob("*.py")):
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            snippet = " ".join(text.strip().splitlines()[:6]).strip()[:220].replace("|", "/")
            mtime = date.fromtimestamp(f.stat().st_mtime).isoformat()
            imported = "yes" if str(f.resolve()) in tracked_paths else "no"
            rows.append((folder.name, f.name, snippet, mtime, imported))

    if not rows:
        return (
            f"No .py files found under {DSA_SOLUTIONS_DIR}"
            + (f" (folder filter: {topic_folder})" if topic_folder else "")
        )

    lines = [f"{len(rows)} file(s) found under {DSA_SOLUTIONS_DIR}:", ""]
    lines.append("| Folder | File | Snippet | Last modified | Imported? |")
    lines.append("|---|---|---|---|---|")
    for folder, fname, snippet, mtime, imported in rows:
        lines.append(f"| {folder} | {fname} | {snippet} | {mtime} | {imported} |")
    lines.append("")
    lines.append(
        "For each row with Imported=no: match it to a catalog id (get_catalog "
        "to browse/search, add_custom_problem if it isn't in the catalog), "
        "then call import_solved_dsa_problem(s)."
    )
    return "\n".join(lines)


@mcp.tool()
def import_solved_dsa_problem(problem_id: str, file_path: str, topic: str = "", difficulty: str = "") -> str:
    """Backfill the DSA tracker from a solution file that already exists on
    disk (found via scan_dsa_directory), WITHOUT modifying the file. Uses the
    file's last-modified time as the 'last practiced' date. Use once per file
    during initial import, or whenever scan_dsa_directory shows a new
    unimported file.

    Args:
        problem_id: Catalog id this file solves (see get_catalog). If it's
            not a built-in catalog problem, call add_custom_problem first.
        file_path: Path to the .py file, absolute or relative to
            DSA_SOLUTIONS_DIR (exactly as shown by scan_dsa_directory).
        topic: Topic tag, only used as a fallback if problem_id isn't in the catalog.
        difficulty: Easy/Medium/Hard, only used as a fallback if problem_id isn't in the catalog.
    """
    problem_id = _slugify(problem_id)
    path = Path(file_path).expanduser()
    if not path.is_absolute():
        path = DSA_SOLUTIONS_DIR / file_path
    if not path.exists():
        return f"File not found: {path}"

    # Guardrail: only ever link to files inside DSA_SOLUTIONS_DIR. Without
    # this, an arbitrary file_path (e.g. "/etc/passwd" or "~/.ssh/id_rsa")
    # would get linked into the tracker and become readable via
    # get_practice_doc -- this keeps the tool scoped to the solutions repo
    # it's meant to index, not the whole filesystem.
    if not path.resolve().is_relative_to(DSA_SOLUTIONS_DIR.resolve()):
        return f"Refusing to import a file outside DSA_SOLUTIONS_DIR ({DSA_SOLUTIONS_DIR}): {path}"

    catalog_entry = {c["id"]: c for c in _load_catalog()["DSA"]}.get(problem_id)
    index = _load_index()
    tracker = index["problems"]["DSA"]
    existing = tracker.get(problem_id, {})
    mtime_date = date.fromtimestamp(path.stat().st_mtime).isoformat()
    verdict = existing.get("last_verdict", "Solved (imported)")

    tracker[problem_id] = {
        "title": catalog_entry["title"] if catalog_entry else existing.get("title", problem_id),
        "topic": catalog_entry["topic"] if catalog_entry else (topic or existing.get("topic", "")),
        "difficulty": catalog_entry["difficulty"] if catalog_entry else (difficulty or existing.get("difficulty", "")),
        "times_practiced": existing.get("times_practiced", 0) + 1,
        "last_practiced": mtime_date,
        "last_verdict": verdict,
        "history": existing.get("history", []) + [{"date": mtime_date, "verdict": verdict}],
        "doc_path": str(path.resolve()),
    }
    _save_index(index)
    return f"Imported `{problem_id}` — linked to {path} (last practiced: {mtime_date})."


@mcp.tool()
def import_solved_dsa_problems(mappings: List[dict]) -> str:
    """Bulk version of import_solved_dsa_problem — import many solved DSA
    files from disk in one call, e.g. right after scan_dsa_directory when
    you've matched several files to catalog ids at once.

    Args:
        mappings: List of dicts, each with keys: problem_id, file_path, and
            optionally topic, difficulty. Same meaning as
            import_solved_dsa_problem's arguments.
    """
    results = []
    for m in mappings:
        results.append(import_solved_dsa_problem(
            m.get("problem_id", ""),
            m.get("file_path", ""),
            m.get("topic", ""),
            m.get("difficulty", ""),
        ))
    return "\n".join(results)


@mcp.tool()
def save_dsa_solution(problem_id: str, title: str, topic: str, code: str, explanation: str = "", overwrite: bool = False) -> str:
    """Save a NEW DSA solution as its own .py file under DSA_SOLUTIONS_DIR,
    matching a typical personal-practice convention: one file per problem,
    grouped in a topic folder, with the problem statement (and optionally
    your explanation) in a module docstring followed by the solution code.
    Call this after solving a DSA problem together that ISN'T already on
    disk — check first with scan_dsa_directory or get_catalog's status
    column.

    Refuses to overwrite an existing file unless overwrite=True is passed:
    this directory holds hand-written personal solutions, so silently
    clobbering one is treated as unsafe by default.

    Args:
        problem_id: Catalog id (see get_catalog), or a slug for a custom
            problem (call add_custom_problem first to register it properly).
        title: Problem title, used in the docstring header and to derive the
            filename.
        topic: Topic tag (e.g. "Graphs", "1-D DP") — determines which topic
            folder the file is placed in (see DSA_FOLDER_MAP in this file
            for the exact mapping; unmapped topics get a new folder).
        code: The full Python solution.
        explanation: Optional prose (approach, complexity) added to the
            docstring above the problem statement.
        overwrite: Set True to replace an existing file for this problem id.
    """
    problem_id = _slugify(problem_id)
    folder = _dsa_folder_name(topic)
    filename = _slugify(title).replace("-", "_") + ".py"
    path = DSA_SOLUTIONS_DIR / folder / filename

    # Guardrail: folder/filename are already built from sanitized inputs
    # (_dsa_folder_name only emits [A-Za-z0-9_], _slugify only [a-z0-9-]), so
    # this can't actually fire today -- kept as defense-in-depth in case
    # either helper changes later.
    if not path.resolve().is_relative_to(DSA_SOLUTIONS_DIR.resolve()):
        return f"Refusing to write outside {DSA_SOLUTIONS_DIR}."

    if path.exists() and not overwrite:
        return (
            f"{path} already exists and overwrite was not set. Pass "
            "overwrite=True if you intend to replace it, or use a different "
            "title if this is meant to be a distinct file."
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    parts = [f'"""\nProblem: {title.strip()}\n']
    if explanation.strip():
        parts.append(f"\n{explanation.strip()}\n")
    parts.append('"""\n\n')
    parts.append(code.strip() + "\n")
    path.write_text("".join(parts), encoding="utf-8")

    catalog_entry = {c["id"]: c for c in _load_catalog()["DSA"]}.get(problem_id)
    index = _load_index()
    tracker = index["problems"]["DSA"]
    existing = tracker.get(problem_id, {})
    today = date.today().isoformat()
    verdict = existing.get("last_verdict", "n/a")

    tracker[problem_id] = {
        "title": title.strip(),
        "topic": catalog_entry["topic"] if catalog_entry else topic.strip(),
        "difficulty": catalog_entry["difficulty"] if catalog_entry else existing.get("difficulty", ""),
        "times_practiced": existing.get("times_practiced", 0) + 1,
        "last_practiced": today,
        "last_verdict": verdict,
        "history": existing.get("history", []) + [{"date": today, "verdict": verdict}],
        "doc_path": str(path.resolve()),
    }
    _save_index(index)

    return f"Saved DSA solution for `{problem_id}` to {path}."


if __name__ == "__main__":
    mcp.run(transport="stdio")
