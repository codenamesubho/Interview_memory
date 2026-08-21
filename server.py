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
  LLD_SOLUTIONS_DIR/<category>/<problem>.py
                                  The LLD reference corpus: one self-contained
                                  .py design per problem, grouped by category
                                  folder. save_practice_doc also writes flat
                                  <id>.md docs here for design write-ups.
  LLD_SOLUTIONS_DIR/Mock Solutions/<id>/
                                  The mock-interview loop: problem.md (posed by
                                  Claude), attempt.py (the USER's own work --
                                  never written or overwritten by this server),
                                  evaluation.md, ideal.py and the pared-back
                                  simple.py (Claude's). Later rounds are
                                  suffixed: attempt_2.py, ideal_2.py, etc.
                                  feedback.md at the top of Mock Solutions/ is
                                  the running rubric scorecard.
  LLD_SOLUTIONS_DIR/DRILL_LOG.md  One running, append-only log of short LLD
                                  drills (the LLD-drill skill) -- quick reps
                                  that don't warrant a whole mock folder.
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

get_current_time() reports this machine's wall clock (local, UTC, epoch) --
call it to time a rep or to check today's date, rather than assuming either.

Typical flow:
  1. get_progress_summary()                     -- orient at the start
  2. suggest_next_problems("DSA")                -- or "HLD" / "LLD"
  3. ... work the problem with the user ...
  4. log_session(..., problem_id=...)             -- always, to update tracker
  5a. save_practice_doc(...)                      -- HLD/LLD: per-problem doc
  5b. save_dsa_solution(...)                      -- DSA: per-problem .py file

LLD mock-interview loop (the user attempts a problem, Claude grades it):
  1. get_lld_feedback()          -- which rubric dimensions are weakest
  2. suggest_next_problems("LLD") -- pick a problem that forces them
  3. start_mock_attempt(...)      -- pose it; the user writes attempt.py
  4. read_lld_solution(...)       -- read what they wrote
  5. save_mock_evaluation(...)    -- score it against LLD_RUBRIC; this logs the
                                     session and regenerates feedback.md
  6. save_ideal_solution(...)     -- the reference design, beside their attempt
  7. save_simple_solution(...)    -- optional: the same design pared back to
                                     what fits in the interview's time box
The rubric vocabulary is fixed (LLD_RUBRIC) so scores aggregate across
sessions -- that aggregate is what step 1 reads, closing the loop.

LLD drills (short focused reps, no rubric, no per-problem folder):
  1. get_lld_drill_log()   -- what was drilled recently, what's unresolved
  2. ... run the drill with the user ...  (get_current_time at both ends
                               gives the duration_minutes below)
  3. log_lld_drill(...)     -- append it to DRILL_LOG.md; gaps feed the same
                               weak-area tracker log_session writes to.

One-time / occasional housekeeping:
  scan_dsa_directory() / scan_lld_directory() -> match files to catalog ids
  yourself -> import_solved_dsa_problem(s) / import_solved_lld_problem(s) to
  backfill the tracker without touching any files.
"""

import json
import os
import re
from datetime import date, datetime, timezone
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

# --- LLD: reference corpus + mock-interview loop -----------------------------
# LLD_SOLUTIONS_DIR holds TWO separate things:
#   1. the reference corpus -- one self-contained .py per problem inside
#      numbered category folders (1_state_machine/, 8_rate_limiting_caching/,
#      ...), hand-written by the user, plus aggregate docs at the top level.
#   2. Mock Solutions/ -- the mock-interview loop, one sub-folder per problem
#      holding the user's own attempt alongside Claude's evaluation and ideal
#      solution. Kept out of every corpus scan/import so an ungraded attempt
#      (or Claude's own output) is never mistaken for the user's finished work.
LLD_MOCK_DIRNAME = "Mock Solutions"
LLD_MOCK_DIR = LLD_SOLUTIONS_DIR / LLD_MOCK_DIRNAME
LLD_FEEDBACK_MD = LLD_MOCK_DIR / "feedback.md"

# The drill log: one running, append-only file for short focused LLD drills
# (the LLD-drill skill), as opposed to the full mock loop under Mock Solutions/.
# Same shape as revision.md -- newest entry at the bottom, sections separated by
# a "---" rule -- but scoped to LLD and kept beside the corpus it's about.
LLD_DRILL_LOG_MD = LLD_SOLUTIONS_DIR / "DRILL_LOG.md"
LLD_DRILL_SEPARATOR = "\n---\n"

# Roles within one mock problem folder, and the extension each is written with.
# "attempt" is the user's own file: no tool in this server ever writes over one.
# "simple" is the pared-back companion to "ideal" -- same problem, only what a
# strong candidate could realistically produce inside the interview's time box.
LLD_MOCK_ROLES = {"problem": "md", "attempt": "py", "evaluation": "md", "ideal": "py", "simple": "py"}

# Fixed scoring vocabulary. Rubric keys MUST come from this list -- free-form
# keys would never aggregate across sessions, which is the whole point of
# tracking them (see get_lld_feedback / suggest_next_problems). Stored in
# index["competency_scores"] under an "lld:" prefix so LLD rubric scores don't
# pollute the behavioral competency averages sharing that namespace.
LLD_RUBRIC = [
    "requirements-and-scope",
    "class-decomposition",
    "design-patterns",
    "solid-and-extensibility",
    "concurrency-and-edge-cases",
    "code-quality",
    "tradeoff-communication",
]
LLD_RUBRIC_PREFIX = "lld:"

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


def _now() -> datetime:
    """The current local wall-clock time, timezone-aware. Local (not UTC) so
    its .date() always agrees with the date.today() every log writer stamps --
    a UTC clock would report a different day either side of midnight and make
    get_current_time contradict revision.md."""
    return datetime.now().astimezone()


def _days_since(iso_date: str) -> int:
    try:
        return (date.today() - date.fromisoformat(iso_date)).days
    except ValueError:
        return 0


def _doc_path(problem_type: str, slug: str) -> Path:
    """HLD/LLD docs are flat: HLD_SOLUTIONS_DIR/<id>.md, LLD_SOLUTIONS_DIR/<id>.md."""
    return SOLUTION_ROOTS[problem_type] / f"{slug}.md"


# ---------------------------------------------------------------------------
# Helpers: the per-problem tracker + on-disk solution directories
# ---------------------------------------------------------------------------

def _catalog_entry(problem_type: str, slug: str) -> dict:
    """The catalog record for one id, or {} if it's not a cataloged problem."""
    return {c["id"]: c for c in _load_catalog()[problem_type]}.get(slug) or {}


def _tracked_doc_paths(problem_type: str) -> set:
    """Absolute paths already linked into the tracker for this type, used by
    the scan tools to mark which files on disk are known."""
    return {
        str(Path(info["doc_path"]).resolve())
        for info in _load_index()["problems"][problem_type].values()
        if info.get("doc_path")
    }


def _record_practice(
    problem_type: str,
    slug: str,
    doc_path: "Path | None",
    when: str,
    title: str = "",
    topic: str = "",
    difficulty: str = "",
    default_verdict: str = "n/a",
) -> dict:
    """Upsert one problem in the tracker, counting this as an attempt and
    linking it to a file on disk. Catalog metadata always wins over the
    caller's title/topic/difficulty, which are fallbacks for problems not in
    the catalog; a previously recorded verdict wins over default_verdict.

    doc_path=None means "this practice produced no per-problem file" (a drill,
    logged into the shared DRILL_LOG.md) -- any doc_path already recorded is
    kept, so logging a drill never unlinks a doc save_practice_doc wrote.

    Shared by save_dsa_solution / save_lld_solution, the import tools and
    log_lld_drill, which differ only in their root directory and default
    verdict.
    """
    index = _load_index()
    tracker = index["problems"][problem_type]
    existing = tracker.get(slug, {})
    catalog = _catalog_entry(problem_type, slug)
    verdict = existing.get("last_verdict", default_verdict)

    tracker[slug] = {
        "title": catalog.get("title") or title.strip() or existing.get("title", slug),
        "topic": catalog.get("topic") or topic.strip() or existing.get("topic", ""),
        "difficulty": catalog.get("difficulty") or difficulty.strip() or existing.get("difficulty", ""),
        "times_practiced": existing.get("times_practiced", 0) + 1,
        "last_practiced": when,
        "last_verdict": verdict,
        "history": existing.get("history", []) + [{"date": when, "verdict": verdict}],
        "doc_path": str(doc_path.resolve()) if doc_path else existing.get("doc_path"),
    }
    _save_index(index)
    return tracker[slug]


def _bump_weak_areas(index: dict, gaps: str) -> List[str]:
    """Fold a semicolon-separated gaps string into index["weak_areas"], the
    running count of what keeps going wrong. Returns the parsed gaps. Shared by
    log_session and log_lld_drill so both feed the same tracker the same way --
    the caller still owns whatever else it writes (revision.md, sessions)."""
    gap_list = [g.strip() for g in gaps.split(";") if g.strip()]
    for g in gap_list:
        key = g.lower()
        index["weak_areas"][key] = index["weak_areas"].get(key, 0) + 1
    return gap_list


def _markdown_table(headers: List[str], rows) -> List[str]:
    """Render a markdown table as a list of lines. Every tool here returns
    markdown to Claude, so this keeps the header/separator/row formatting in
    one place."""
    return [
        "| " + " | ".join(headers) + " |",
        "|" + "---|" * len(headers),
        *("| " + " | ".join(str(c) for c in row) + " |" for row in rows),
    ]


def _scan_row(f: Path) -> tuple:
    """(snippet, last-modified) for one file in a solutions directory, or
    None if it can't be read. The snippet is the first few lines — usually the
    module docstring naming the problem — clipped and stripped of pipes so it
    can't break the markdown table it's rendered into."""
    try:
        text = f.read_text(encoding="utf-8", errors="ignore")
        mtime = date.fromtimestamp(f.stat().st_mtime).isoformat()
    except OSError:
        return None
    snippet = " ".join(text.strip().splitlines()[:6]).strip()[:220].replace("|", "/")
    return snippet, mtime


def _import_solved_problem(
    problem_type: str,
    problem_id: str,
    file_path: str,
    topic: str = "",
    difficulty: str = "",
    solutions_only: bool = False,
) -> str:
    """Link an existing on-disk solution file to a catalog id WITHOUT modifying
    the file, using its mtime as the 'last practiced' date. Shared by the DSA
    and LLD import tools.

    solutions_only applies the LLD kind check, so an aggregate doc or one of
    Claude's own mock-interview files can't be recorded as the user's finished
    work.
    """
    root = SOLUTION_ROOTS[problem_type]
    problem_id = _slugify(problem_id)
    path = Path(file_path).expanduser()
    if not path.is_absolute():
        path = root / file_path
    if not path.exists():
        return f"File not found: {path}"

    # Guardrail: only ever link to files inside this type's solutions root.
    # Without it, an arbitrary file_path (e.g. "/etc/passwd" or "~/.ssh/id_rsa")
    # would get linked into the tracker and become readable via get_practice_doc.
    try:
        inside = path.resolve().is_relative_to(root.resolve())
    except OSError:
        inside = False
    if not inside:
        return f"Refusing to import a file outside {problem_type}_SOLUTIONS_DIR ({root}): {path}"

    if solutions_only:
        kind = _lld_kind(path)
        if kind != "solution":
            return (
                f"Refusing to import {path.name}: kind is `{kind}`, not `solution`. "
                "Only per-problem designs in a category folder can be imported "
                "(mock-interview files are tracked by save_mock_evaluation instead)."
            )

    mtime_date = date.fromtimestamp(path.stat().st_mtime).isoformat()
    _record_practice(
        problem_type, problem_id, path, mtime_date,
        topic=topic, difficulty=difficulty, default_verdict="Solved (imported)",
    )
    return f"Imported `{problem_id}` — linked to {path} (last practiced: {mtime_date})."


def _bulk_import(problem_type: str, mappings: List[dict], solutions_only: bool = False) -> str:
    """Run _import_solved_problem over a list of {problem_id, file_path, ...}."""
    return "\n".join(
        _import_solved_problem(
            problem_type,
            m.get("problem_id", ""),
            m.get("file_path", ""),
            m.get("topic", ""),
            m.get("difficulty", ""),
            solutions_only=solutions_only,
        )
        for m in mappings
    )


# ---------------------------------------------------------------------------
# Helpers: LLD corpus layout + mock-interview folders
# ---------------------------------------------------------------------------

def _in_lld_root(path: Path) -> bool:
    """True if path is inside LLD_SOLUTIONS_DIR. Every LLD tool that takes a
    caller-supplied path routes through this -- a relative path like
    "../../.ssh/id_rsa", or an absolute path anywhere else on disk, must never
    be readable or writable through these tools."""
    try:
        return path.resolve().is_relative_to(LLD_SOLUTIONS_DIR.resolve())
    except OSError:
        return False


def _is_mock_path(path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(LLD_MOCK_DIR.resolve())
    except OSError:
        return False


def _has_practice_doc_header(path: Path) -> bool:
    """True if this markdown file was written by save_practice_doc, which
    stamps "_Last updated: <date> · Type: <type> · id: `<slug>`_" under the
    title. Only the head of the file is read."""
    try:
        with path.open(encoding="utf-8", errors="ignore") as f:
            head = f.read(400)
    except OSError:
        return False
    return "· Type: " in head and "· id: " in head


def _lld_kind(path: Path) -> str:
    """Classify one file under LLD_SOLUTIONS_DIR so callers can tell the user's
    own designs apart from aggregate docs and from Claude's generated output.
    Returned verbatim in scan_lld_directory's Kind column; the import tools
    refuse anything that isn't a 'solution'."""
    name = path.name
    stem = path.stem.lower()

    if _is_mock_path(path):
        if name == LLD_FEEDBACK_MD.name:
            return "mock-feedback"
        for role in LLD_MOCK_ROLES:
            # Rounds are suffixed (attempt_2.py), so match on the stem prefix.
            if stem == role or stem.startswith(f"{role}_"):
                return f"mock-{role}"
        return "mock-other"

    if path.suffix.lower() != ".py":
        if path.suffix.lower() in {".md", ".txt"}:
            # A .md inside a category folder documents that category.
            if path.parent != LLD_SOLUTIONS_DIR:
                return "category-doc"
            # The drill log is generated (and appended to) by this server, like
            # feedback.md -- not a corpus-wide index the user maintains.
            if name == LLD_DRILL_LOG_MD.name:
                return "drill-log"
            # At the top level, save_practice_doc's own per-problem write-ups
            # sit alongside corpus-wide indexes (INDEX.md, QUICK_REFERENCE.md).
            # Tell them apart by the header that tool stamps on every file it
            # writes, so a write-up about one problem isn't reported as
            # spanning many.
            return "practice-doc" if _has_practice_doc_header(path) else "aggregate-doc"
        return "other"

    # A .py directly at the top level isn't part of the by-category convention.
    return "solution" if path.parent != LLD_SOLUTIONS_DIR else "other"


def _mock_dir(problem_id: str) -> Path:
    """Mock Solutions/<slug>/ -- slug always via _slugify, so a problem_id like
    "../../etc" can't escape the mock directory."""
    return LLD_MOCK_DIR / _slugify(problem_id)


def _mock_file(problem_id: str, role: str, round_no: int = 1) -> Path:
    """Path for one role within a mock problem folder. Round 1 is unsuffixed
    (attempt.py); later rounds are suffixed (attempt_2.py). The role is looked
    up in LLD_MOCK_ROLES rather than interpolated, so the filename is fixed by
    construction -- that's what makes it impossible for save_ideal_solution to
    ever target the user's attempt.py."""
    if role not in LLD_MOCK_ROLES:
        raise ValueError(f"unknown mock role {role!r}")
    suffix = "" if round_no <= 1 else f"_{round_no}"
    return _mock_dir(problem_id) / f"{role}{suffix}.{LLD_MOCK_ROLES[role]}"


def _mock_rounds(problem_id: str) -> List[int]:
    """Round numbers that have an attempt file on disk, ascending."""
    folder = _mock_dir(problem_id)
    if not folder.is_dir():
        return []
    rounds = []
    for f in folder.glob("attempt*.py"):
        stem = f.stem
        if stem == "attempt":
            rounds.append(1)
        elif stem.startswith("attempt_") and stem[len("attempt_"):].isdigit():
            rounds.append(int(stem[len("attempt_"):]))
    return sorted(rounds)


def _resolve_round(problem_id: str, round_no: int) -> int:
    """round_no=0 means 'the latest round that has an attempt' (1 if none yet)."""
    if round_no > 0:
        return round_no
    rounds = _mock_rounds(problem_id)
    return rounds[-1] if rounds else 1


def _lld_category_folder(category: str) -> str:
    """Resolve a category name to an existing numbered folder in the corpus
    (substring match, so "state machine", "state_machine" and
    "1_state_machine" all land in 1_state_machine/). Falls back to a
    sanitized new folder name when nothing matches."""
    key = re.sub(r"[^a-z0-9]+", "", category.lower())
    if LLD_SOLUTIONS_DIR.exists() and key:
        for folder in sorted(p for p in LLD_SOLUTIONS_DIR.iterdir() if p.is_dir()):
            if folder.name in IGNORE_DIRS or folder.name == LLD_MOCK_DIRNAME:
                continue
            # Compare ignoring the numeric prefix and separators:
            # "1_state_machine" -> "1statemachine", also matched by "statemachine".
            normalized = re.sub(r"[^a-z0-9]+", "", folder.name.lower())
            if key == normalized or key in normalized or normalized.lstrip("0123456789") == key:
                return folder.name
    words = re.findall(r"[A-Za-z0-9]+", category)
    return "_".join(w.lower() for w in words) if words else "misc"


def _ensure_drill_log() -> None:
    """Create DRILL_LOG.md with its preamble if it isn't there yet. The LLD
    root is env-configurable and may not exist at all, so make it too --
    same contract as _ensure_revision_md."""
    LLD_SOLUTIONS_DIR.mkdir(parents=True, exist_ok=True)
    if not LLD_DRILL_LOG_MD.exists():
        LLD_DRILL_LOG_MD.write_text(
            "# LLD Drill Log\n\n"
            "Short, focused LLD drills — appended by the interview-memory MCP "
            "server (log_lld_drill). Newest entries are at the bottom. Full "
            "mock interviews live under "
            f"{LLD_MOCK_DIRNAME}/ instead.\n",
            encoding="utf-8",
        )


def _rubric_scores() -> dict:
    """The LLD rubric slice of index["competency_scores"], keys un-prefixed."""
    scores = _load_index().get("competency_scores", {})
    return {
        k[len(LLD_RUBRIC_PREFIX):]: v
        for k, v in scores.items()
        if k.startswith(LLD_RUBRIC_PREFIX)
    }


def _rated_dimensions() -> List[tuple]:
    """(dimension, avg, count) for every scored rubric dimension, worst first."""
    rated = [(k, v["avg"], v["count"]) for k, v in _rubric_scores().items() if v.get("count")]
    rated.sort(key=lambda kv: kv[1])
    return rated


def _unrated_dimensions() -> List[str]:
    """Rubric dimensions no attempt has exercised yet."""
    scored = _rubric_scores()
    return [d for d in LLD_RUBRIC if d not in scored]


def _weakest_dimensions(limit: int = 3) -> List[tuple]:
    """The lowest-scoring rubric dimensions — what the next mock should target."""
    return _rated_dimensions()[:limit]


def _mock_history_rows(records: List[dict], limit: int = 0) -> List[tuple]:
    """Attempt-history table rows, oldest first, optionally only the last N."""
    ordered = sorted(records, key=lambda r: (r["date"], r["round"]))
    if limit:
        ordered = ordered[-limit:]
    rows = []
    for rec in ordered:
        scores = rec.get("scores", {})
        weakest = min(scores.items(), key=lambda kv: kv[1])[0] if scores else "—"
        rows.append((
            rec["date"],
            f"{rec['title']} (`{rec['problem_id']}`)",
            rec["round"],
            rec["verdict"],
            f"{rec.get('average', 0):.1f}/5",
            weakest,
        ))
    return rows


# ---------------------------------------------------------------------------
# Tools: the clock
# ---------------------------------------------------------------------------

@mcp.tool()
def get_current_time() -> str:
    """Read the current wall-clock time on this machine. Call this whenever
    the actual clock matters rather than your own assumption about it:

      - timing a rep: call it when the user starts and again when they
        finish, and pass the difference as log_lld_drill(duration_minutes=...)
        or as the time box in a mock evaluation;
      - answering "how long did that take" / "how much time is left" during a
        timed mock;
      - checking today's date before talking about revision scheduling (this
        is the same date every log writer here stamps).

    Returns the local time, the UTC equivalent and the epoch seconds. The
    local date is authoritative: log_session, log_lld_drill and
    save_practice_doc all stamp that date, so quote it (not the UTC one) when
    referring to "today".
    """
    now = _now()
    return "\n".join([
        f"Local: {now.isoformat(timespec='seconds')} ({now.strftime('%A, %d %B %Y, %H:%M:%S %Z')})",
        f"UTC:   {now.astimezone(timezone.utc).isoformat(timespec='seconds')}",
        f"Epoch: {int(now.timestamp())}",
        f"Today (as stamped in revision.md / DRILL_LOG.md): {now.date().isoformat()}",
    ])


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

    # Behavioral competencies and LLD rubric dimensions share this dict but are
    # different scales measuring different things, so they're reported apart --
    # LLD keys carry the LLD_RUBRIC_PREFIX (see save_mock_evaluation).
    competency_scores = index.get("competency_scores", {})
    behavioral = {k: v for k, v in competency_scores.items() if not k.startswith(LLD_RUBRIC_PREFIX)}
    if behavioral:
        lines += ["", "Competency scores (behavioral, 1-5 avg):"]
        for area, stats in sorted(behavioral.items(), key=lambda kv: kv[1]["avg"]):
            lines.append(f"- {area}: {stats['avg']:.1f} ({stats['count']} rated)")

    rated = _rated_dimensions()
    if rated:
        lines += ["", "LLD mock rubric (1-5 avg, weakest first):"]
        lines += [f"- {dim}: {avg:.1f} ({n} rated)" for dim, avg, n in rated]
        lines.append("Call get_lld_feedback before an LLD session for the full scorecard.")

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
    gap_list = _bump_weak_areas(index, gaps)
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
    lines += _markdown_table(
        ["id", "Title", "Topic", "Difficulty", "Status", "Why"],
        (
            (f"`{c['id']}`", c["title"], c["topic"], c["difficulty"], c["status"], c["reason"])
            for c in top
        ),
    )

    # Ranking above matches weak areas against the catalog's topic/title
    # strings. That works for DSA (topics like "Graphs" are what gets flagged)
    # but not for LLD, where every topic starts "OOP Design" and the real
    # weaknesses are rubric dimensions like class-decomposition. So surface
    # those separately and let the interviewer weigh them when picking.
    if problem_type == "LLD":
        weakest = _weakest_dimensions()
        if weakest:
            lines += [
                "",
                "Weakest LLD rubric dimensions so far: "
                + ", ".join(f"{d} ({avg:.1f}/5)" for d, avg, _ in weakest)
                + ". Prefer a problem that forces these, and probe them during "
                "the interview. get_lld_feedback has the full scorecard.",
            ]
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

    tracked_paths = _tracked_doc_paths("DSA")

    rows = []
    for folder in sorted(p for p in DSA_SOLUTIONS_DIR.iterdir() if p.is_dir()):
        if folder.name in IGNORE_DIRS or folder.name.startswith("."):
            continue
        if topic_folder and topic_folder.lower() not in folder.name.lower():
            continue
        for f in sorted(folder.glob("*.py")):
            row = _scan_row(f)
            if row is None:
                continue
            snippet, mtime = row
            imported = "yes" if str(f.resolve()) in tracked_paths else "no"
            rows.append((folder.name, f.name, snippet, mtime, imported))

    if not rows:
        return (
            f"No .py files found under {DSA_SOLUTIONS_DIR}"
            + (f" (folder filter: {topic_folder})" if topic_folder else "")
        )

    lines = [f"{len(rows)} file(s) found under {DSA_SOLUTIONS_DIR}:", ""]
    lines += _markdown_table(["Folder", "File", "Snippet", "Last modified", "Imported?"], rows)
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
    return _import_solved_problem("DSA", problem_id, file_path, topic, difficulty)


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
    return _bulk_import("DSA", mappings)


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

    _record_practice("DSA", problem_id, path, date.today().isoformat(), title=title, topic=topic)
    return f"Saved DSA solution for `{problem_id}` to {path}."


# ---------------------------------------------------------------------------
# Tools: LLD reference corpus (scan existing designs, read one, import, save new)
# ---------------------------------------------------------------------------

@mcp.tool()
def scan_lld_directory(category_folder: str = "") -> str:
    """Scan the on-disk LLD solutions directory (LLD_SOLUTIONS_DIR — the
    user's real design repo, organized as <category-folder>/<problem>.py) to
    see what's ACTUALLY been designed, independent of whether it was ever
    logged through this MCP. Returns each file's category folder, filename,
    kind, a snippet from the top of the file, and its last-modified date,
    plus whether it's already linked in the tracker.

    The Kind column matters — only rows with kind=solution are the user's
    per-problem designs. category-doc / aggregate-doc rows are READMEs and
    index files spanning many problems; practice-doc rows are markdown
    write-ups this server wrote via save_practice_doc (fetch those with
    get_practice_doc). Don't import anything but solutions. Files under
    "Mock Solutions/" are deliberately excluded here: use list_mock_attempts
    for the mock-interview loop.

    Read the snippets, match each not-yet-imported solution to a catalog id
    yourself (browse with get_catalog; add_custom_problem first for anything
    not in the catalog — roughly half of a typical LLD repo won't be), then
    call import_solved_lld_problem or import_solved_lld_problems. Use
    read_lld_solution to open any single file in full.

    Args:
        category_folder: Optional substring filter on the category folder
            name (e.g. "state_machine", "game").
    """
    if not LLD_SOLUTIONS_DIR.exists():
        return (
            f"LLD_SOLUTIONS_DIR does not exist: {LLD_SOLUTIONS_DIR}. "
            "Set the LLD_SOLUTIONS_DIR env var in your MCP config to point "
            "at your existing design repo, or create the directory."
        )

    tracked_paths = _tracked_doc_paths("LLD")
    rows = []

    def add(folder_label: str, f: Path) -> None:
        row = _scan_row(f)
        if row is None:
            return
        snippet, mtime = row
        kind = _lld_kind(f)
        imported = "yes" if str(f.resolve()) in tracked_paths else ("no" if kind == "solution" else "n/a")
        rows.append((folder_label, f.name, kind, snippet, mtime, imported))

    for folder in sorted(p for p in LLD_SOLUTIONS_DIR.iterdir() if p.is_dir()):
        if folder.name in IGNORE_DIRS or folder.name.startswith(".") or folder.name == LLD_MOCK_DIRNAME:
            continue
        if category_folder and category_folder.lower() not in folder.name.lower():
            continue
        for f in sorted(folder.iterdir()):
            if f.is_file() and not f.name.startswith("."):
                add(folder.name, f)

    # Top-level files are the corpus-wide docs (INDEX.md, QUICK_REFERENCE.md).
    # Only listed when unfiltered, so a category filter stays clean.
    if not category_folder:
        for f in sorted(p for p in LLD_SOLUTIONS_DIR.iterdir() if p.is_file()):
            if not f.name.startswith("."):
                add("(top level)", f)

    if not rows:
        return (
            f"No files found under {LLD_SOLUTIONS_DIR}"
            + (f" (folder filter: {category_folder})" if category_folder else "")
        )

    solutions = sum(1 for r in rows if r[2] == "solution")
    lines = [
        f"{len(rows)} file(s) under {LLD_SOLUTIONS_DIR} — {solutions} of kind `solution`:",
        "",
    ]
    lines += _markdown_table(["Folder", "File", "Kind", "Snippet", "Last modified", "Imported?"], rows)
    lines += [
        "",
        "For each kind=solution row with Imported=no: match it to a catalog id "
        "(get_catalog to browse, add_custom_problem if it isn't there), then "
        "call import_solved_lld_problem(s). read_lld_solution opens one file "
        "in full.",
    ]
    return "\n".join(lines)


@mcp.tool()
def read_lld_solution(relative_path: str) -> str:
    """Read ONE file under LLD_SOLUTIONS_DIR in full — a design from the
    reference corpus (e.g. "4_game_design/chess_game.py"), one of the
    corpus-wide docs, or a file from a mock-interview folder (e.g.
    "Mock Solutions/design-parking-lot/attempt.py"). Use this to actually
    look at the user's work: to review a past design, to compare an approach,
    or — most importantly — to read a mock attempt before evaluating it.

    Read one file at a time. These are full multi-class designs (up to ~550
    lines); pulling many at once will bury the conversation.

    Args:
        relative_path: Path as shown by scan_lld_directory or
            list_mock_attempts, relative to LLD_SOLUTIONS_DIR (an absolute
            path inside that directory also works).
    """
    path = Path(relative_path).expanduser()
    if not path.is_absolute():
        path = LLD_SOLUTIONS_DIR / relative_path
    if not _in_lld_root(path):
        return f"Refusing to read outside LLD_SOLUTIONS_DIR ({LLD_SOLUTIONS_DIR}): {relative_path}"
    if not path.exists():
        return f"File not found: {path}. Use scan_lld_directory or list_mock_attempts to see what exists."
    if path.is_dir():
        return f"{path} is a directory, not a file. Use scan_lld_directory / list_mock_attempts to list it."
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        return f"Could not read {path}: {e}"
    return f"# {path.relative_to(LLD_SOLUTIONS_DIR)} (kind: {_lld_kind(path)})\n\n{text}"


@mcp.tool()
def import_solved_lld_problem(problem_id: str, file_path: str, topic: str = "", difficulty: str = "") -> str:
    """Backfill the LLD tracker from a design file that already exists on
    disk (found via scan_lld_directory), WITHOUT modifying the file. Uses the
    file's last-modified time as the 'last practiced' date. Use during the
    initial import of an existing design repo, or whenever
    scan_lld_directory shows a new unimported solution.

    Args:
        problem_id: Catalog id this file designs (see get_catalog). If it's
            not a built-in catalog problem, call add_custom_problem first.
        file_path: Path to the .py file, absolute or relative to
            LLD_SOLUTIONS_DIR (exactly as shown by scan_lld_directory).
        topic: Topic tag, only used as a fallback if problem_id isn't in the catalog.
        difficulty: Easy/Medium/Hard, only used as a fallback if problem_id isn't in the catalog.
    """
    # solutions_only: an aggregate doc covers many problems, and a mock-* file
    # is either ungraded or Claude's own output -- neither is the user's
    # finished design for this id.
    return _import_solved_problem("LLD", problem_id, file_path, topic, difficulty, solutions_only=True)


@mcp.tool()
def import_solved_lld_problems(mappings: List[dict]) -> str:
    """Bulk version of import_solved_lld_problem — import many existing LLD
    design files in one call, e.g. right after scan_lld_directory when you've
    matched several files to catalog ids at once.

    Args:
        mappings: List of dicts, each with keys: problem_id, file_path, and
            optionally topic, difficulty. Same meaning as
            import_solved_lld_problem's arguments.
    """
    return _bulk_import("LLD", mappings, solutions_only=True)


@mcp.tool()
def save_lld_solution(problem_id: str, title: str, category: str, code: str, explanation: str = "", overwrite: bool = False) -> str:
    """Save a NEW LLD design as its own .py file in the reference corpus under
    LLD_SOLUTIONS_DIR, matching the existing convention: one self-contained
    file per problem inside a numbered category folder, with the problem and
    the design notes in a module docstring above the classes. Use this for a
    design the user wants to KEEP as reference material.

    For the mock-interview loop (the user attempts a problem and you grade
    it), use start_mock_attempt / save_mock_evaluation / save_ideal_solution
    instead — those write under "Mock Solutions/" and never touch this corpus.

    Refuses to overwrite an existing file unless overwrite=True: this
    directory holds hand-written personal designs, so silently clobbering one
    is treated as unsafe by default.

    Args:
        problem_id: Catalog id (see get_catalog), or a slug for a custom
            problem (call add_custom_problem first to register it properly).
        title: Problem title, used in the docstring header and to derive the
            filename.
        category: Category folder to place it in — matched loosely against
            the existing folders, so "state machine", "state_machine" and
            "1_state_machine" all resolve to the same one. An unmatched
            category creates a new folder.
        code: The full Python design (classes, enums, and a demo under
            `if __name__ == "__main__":` to match the corpus convention).
        explanation: Optional prose (patterns used, key decisions) added to
            the docstring above the code.
        overwrite: Set True to replace an existing file for this problem.
    """
    problem_id = _slugify(problem_id)
    folder = _lld_category_folder(category)
    filename = _slugify(title).replace("-", "_") + ".py"
    path = LLD_SOLUTIONS_DIR / folder / filename

    if not _in_lld_root(path) or _is_mock_path(path):
        return f"Refusing to write outside the LLD reference corpus ({LLD_SOLUTIONS_DIR})."
    if path.exists() and not overwrite:
        return (
            f"{path} already exists and overwrite was not set. Pass "
            "overwrite=True if you intend to replace it, or use a different "
            "title if this is meant to be a distinct file."
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    parts = [f'"""\n{title.strip()}\n']
    if explanation.strip():
        parts.append(f"\n{explanation.strip()}\n")
    parts.append('"""\n\n')
    parts.append(code.strip() + "\n")
    path.write_text("".join(parts), encoding="utf-8")

    _record_practice("LLD", problem_id, path, date.today().isoformat(), title=title)
    return f"Saved LLD design for `{problem_id}` to {path}."


# ---------------------------------------------------------------------------
# Tools: LLD mock-interview loop (pose -> user attempts -> score -> ideal)
# ---------------------------------------------------------------------------

@mcp.tool()
def start_mock_attempt(problem_id: str, title: str, prompt_markdown: str) -> str:
    """Pose an LLD problem for the user to attempt on their own. Creates
    "Mock Solutions/<problem-id>/" containing problem.md (the prompt exactly
    as you posed it, including requirements and any constraints you want them
    to handle) and an empty attempt.py stub for them to fill in.

    Call this at the START of a mock interview, after picking the problem
    with get_lld_feedback + suggest_next_problems. Then stop and let the user
    write. When they say they're done, read_lld_solution the attempt, then
    save_mock_evaluation, save_ideal_solution, and optionally
    save_simple_solution for the pared-back version.

    Never overwrites an existing attempt.py. Calling this again for a problem
    that already has an attempt opens the NEXT round (attempt_2.py, ...), so
    re-attempting a problem later keeps the earlier round for comparison.

    Args:
        problem_id: Catalog id (see get_catalog) or a slug for a custom
            problem — call add_custom_problem first so it's tracked properly.
        title: Problem title, e.g. "Design a Parking Lot".
        prompt_markdown: The full problem statement as posed: scenario,
            functional requirements, constraints, and what you expect them to
            produce. Authored entirely by you; this tool only persists it.
    """
    slug = _slugify(problem_id) if problem_id.strip() else _slugify(title)
    existing_rounds = _mock_rounds(slug)
    round_no = (existing_rounds[-1] + 1) if existing_rounds else 1

    attempt = _mock_file(slug, "attempt", round_no)
    problem = _mock_file(slug, "problem", round_no)
    if not _in_lld_root(attempt):
        return f"Refusing to write outside {LLD_SOLUTIONS_DIR}."
    if attempt.exists():
        return f"{attempt} already exists — refusing to overwrite the user's attempt."

    attempt.parent.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    problem.write_text(
        f"# {title.strip()}\n\n_Posed: {today} · Type: LLD · id: `{slug}` · round {round_no}_\n\n---\n\n"
        + prompt_markdown.strip() + "\n",
        encoding="utf-8",
    )
    attempt.write_text(
        f'"""\n{title.strip()} — mock attempt (round {round_no}, posed {today})\n\n'
        f"Your design goes here. See problem.md in this folder for the prompt.\n"
        f'"""\n',
        encoding="utf-8",
    )

    return (
        f"Round {round_no} of `{slug}` started.\n"
        f"- Prompt: {problem}\n"
        f"- Write your design in: {attempt}\n\n"
        "Tell me when you're done and I'll read it, score it against the "
        "rubric, and write an ideal solution alongside it."
    )


@mcp.tool()
def list_mock_attempts(problem_id: str = "") -> str:
    """List the LLD mock-interview folders under "Mock Solutions/": for each
    problem and round, which files exist (problem / attempt / evaluation /
    ideal / simple), whether the attempt has been graded yet, and the score if
    it has.

    Call this to find work pending evaluation ("did I ever grade that one?"),
    or at the start of a session to see the user's mock history. Use
    read_lld_solution to open any listed file.

    Args:
        problem_id: Optional — limit to one problem's folder.
    """
    if not LLD_MOCK_DIR.exists():
        return (
            f"No mock attempts yet — {LLD_MOCK_DIR} doesn't exist. "
            "Use start_mock_attempt to pose the first problem."
        )

    evaluations = {}
    for rec in _load_index().get("lld_mock", []):
        evaluations[(rec["problem_id"], rec["round"])] = rec

    folders = sorted(p for p in LLD_MOCK_DIR.iterdir() if p.is_dir() and not p.name.startswith("."))
    if problem_id.strip():
        slug = _slugify(problem_id)
        folders = [f for f in folders if f.name == slug]
        if not folders:
            return f"No mock folder for `{slug}` under {LLD_MOCK_DIR}."

    rows = []
    pending = []
    for folder in folders:
        slug = folder.name
        for r in _mock_rounds(slug) or [1]:
            def mark(role: str) -> str:
                return "yes" if _mock_file(slug, role, r).exists() else "—"
            rec = evaluations.get((slug, r))
            rows.append((
                f"`{slug}`", r,
                mark("problem"), mark("attempt"), mark("evaluation"), mark("ideal"), mark("simple"),
                f"{rec['average']:.1f}/5" if rec and rec.get("average") else "—",
                rec["verdict"] if rec else "not graded",
            ))
            if _mock_file(slug, "attempt", r).exists() and not rec:
                pending.append(f"{slug} (round {r})")

    if not rows:
        return f"No mock attempts found under {LLD_MOCK_DIR}. Use start_mock_attempt to pose one."

    out = [f"Mock attempts under {LLD_MOCK_DIR}:", ""]
    out += _markdown_table(
        ["Problem", "Round", "Prompt", "Attempt", "Evaluation", "Ideal", "Simple", "Score", "Verdict"],
        rows,
    )
    if pending:
        out += ["", "Awaiting evaluation: " + ", ".join(pending)]
    return "\n".join(out)


@mcp.tool()
def save_mock_evaluation(
    problem_id: str,
    rubric_scores: dict,
    verdict: str,
    strengths: str,
    gaps: str,
    evaluation_markdown: str,
    title: str = "",
    round_no: int = 0,
) -> str:
    """Score a mock LLD attempt as the interviewer. Writes evaluation.md next
    to the user's attempt, records the rubric scores so they accumulate across
    sessions, regenerates feedback.md, and logs the session.

    Call this AFTER reading the attempt with read_lld_solution. Score honestly
    against what an interviewer would expect at the target level — inflated
    scores make the whole feedback loop useless, since these averages are what
    later sessions use to pick problems.

    Grade each round once. Re-grading the same round appends a second set of
    scores rather than replacing the first, so the averages would count that
    attempt twice.

    rubric_scores keys MUST come from this fixed list (1-5 each; omit a
    dimension the problem didn't exercise):
      requirements-and-scope     — clarifying questions, scoping, assumptions
      class-decomposition        — entity/responsibility split, cohesion
      design-patterns            — pattern choice and whether it was justified
      solid-and-extensibility    — SOLID, open/closed, how it absorbs change
      concurrency-and-edge-cases — thread safety, races, boundary conditions
      code-quality               — naming, structure, idiomatic Python
      tradeoff-communication     — articulating and defending alternatives
    The vocabulary is fixed on purpose: free-form keys would never aggregate,
    and the aggregate is what tunes future question selection.

    Args:
        problem_id: The mock problem's id (as used by start_mock_attempt).
        rubric_scores: dict of dimension -> integer 1-5, keys from the list above.
        verdict: One of Strong Hire, Hire, Lean Hire, Lean No Hire, No Hire.
        strengths: Semicolon-separated list of what the user did well.
        gaps: Semicolon-separated short weak-area tags. Prefer reusing rubric
            dimension names here so they aggregate with the scores.
        evaluation_markdown: Your full written critique — what was missing,
            what an interviewer would have pushed on, what to do differently.
            Authored entirely by you.
        title: Problem title, used if this problem isn't in the catalog yet.
        round_no: Which round to grade. 0 (default) = the latest attempt.
    """
    slug = _slugify(problem_id)
    round_no = _resolve_round(slug, round_no)

    if verdict.strip() not in VALID_VERDICTS:
        return f"Invalid verdict '{verdict}'. Use one of: {sorted(VALID_VERDICTS)}"

    unknown = [k for k in rubric_scores if k.strip().lower() not in LLD_RUBRIC]
    if unknown:
        return (
            f"Unknown rubric dimension(s): {unknown}. Scores only aggregate if "
            f"keys come from the fixed vocabulary: {LLD_RUBRIC}"
        )
    if not rubric_scores:
        return f"rubric_scores is empty — score at least one dimension from {LLD_RUBRIC}."
    clean_scores = {}
    for area, score in rubric_scores.items():
        if not isinstance(score, int) or not (MIN_COMPETENCY_SCORE <= score <= MAX_COMPETENCY_SCORE):
            return (
                f"Invalid score for '{area}': {score!r}. Scores must be "
                f"integers {MIN_COMPETENCY_SCORE}-{MAX_COMPETENCY_SCORE}."
            )
        clean_scores[area.strip().lower()] = score

    attempt = _mock_file(slug, "attempt", round_no)
    if not attempt.exists():
        return (
            f"No attempt found at {attempt}. Use list_mock_attempts to see what "
            "exists, or start_mock_attempt to pose the problem first."
        )

    path = _mock_file(slug, "evaluation", round_no)
    if not _in_lld_root(path):
        return f"Refusing to write outside {LLD_SOLUTIONS_DIR}."

    catalog_entry = {c["id"]: c for c in _load_catalog()["LLD"]}.get(slug)
    display_title = title.strip() or (catalog_entry["title"] if catalog_entry else slug)
    today = date.today().isoformat()
    average = sum(clean_scores.values()) / len(clean_scores)

    # Rendered in LLD_RUBRIC order (not dict order) so every evaluation.md
    # reads the same way regardless of how the scores were passed in.
    score_table = _markdown_table(
        ["Dimension", "Score"],
        [(dim, f"{clean_scores[dim]}/5") for dim in LLD_RUBRIC if dim in clean_scores]
        + [("**average**", f"**{average:.1f}/5**")],
    )

    path.write_text(
        f"# {display_title} — evaluation (round {round_no})\n\n"
        f"_Graded: {today} · Type: LLD · id: `{slug}` · verdict: **{verdict.strip()}**_\n\n"
        f"Attempt: `{attempt.name}`\n\n"
        + "\n".join(score_table)
        + "\n\n---\n\n"
        + evaluation_markdown.strip()
        + "\n",
        encoding="utf-8",
    )

    # Rubric scores go through log_session's competency machinery under an
    # "lld:" prefix, so they aggregate exactly like behavioral competencies
    # without sharing their namespace. log_session also appends to revision.md
    # and updates weak_areas + the per-problem tracker.
    log_result = log_session(
        topic=display_title,
        interview_type="LLD",
        verdict=verdict.strip(),
        strengths=strengths,
        gaps=gaps,
        notes=f"Mock attempt round {round_no}. Attempt: {attempt}. Evaluation: {path}.",
        problem_id=slug,
        competency_scores={f"{LLD_RUBRIC_PREFIX}{k}": v for k, v in clean_scores.items()},
    )

    index = _load_index()
    index.setdefault("lld_mock", []).append({
        "problem_id": slug,
        "title": display_title,
        "round": round_no,
        "date": today,
        "verdict": verdict.strip(),
        "scores": clean_scores,
        "average": average,
        "attempt_path": str(attempt.resolve()),
        "evaluation_path": str(path.resolve()),
    })
    # Point the tracker's doc_path at the evaluation so get_practice_doc pulls
    # up the graded critique (its existing guardrail allows this: Mock
    # Solutions/ lives under LLD_SOLUTIONS_DIR).
    tracker = index["problems"]["LLD"]
    if slug in tracker:
        tracker[slug]["doc_path"] = str(path.resolve())
    _save_index(index)

    feedback_note = _regenerate_lld_feedback()
    weakest = _weakest_dimensions()
    weak_note = (
        " Weakest dimensions now: "
        + ", ".join(f"{d} ({avg:.1f})" for d, avg, _ in weakest)
        + "."
    ) if weakest else ""

    return (
        f"Evaluation saved to {path} (average {average:.1f}/5, verdict {verdict.strip()}).\n"
        f"{log_result}\n{feedback_note}{weak_note}\n"
        "Next: save_ideal_solution to write the reference design alongside the "
        "attempt, then save_simple_solution for the pared-back version."
    )


def _save_mock_solution(
    role: str,
    label: str,
    problem_id: str,
    code: str,
    notes: str,
    round_no: int,
    overwrite: bool,
) -> str:
    """Write one of Claude's own .py files into a mock problem folder, beside
    the user's attempt. Shared by save_ideal_solution and
    save_simple_solution, which differ only in the role they write (and so in
    the filename, docstring header and index key).
    """
    slug = _slugify(problem_id)
    round_no = _resolve_round(slug, round_no)
    path = _mock_file(slug, role, round_no)

    if not _in_lld_root(path) or not _is_mock_path(path):
        return f"Refusing to write outside {LLD_MOCK_DIR}."
    # Belt-and-braces: _mock_file builds the name from LLD_MOCK_ROLES, so this
    # can't fire today -- kept so any future change to that helper can't turn
    # these tools into something that clobbers the user's own work.
    if not path.name.startswith(role):
        return f"Refusing to write {path.name}: this tool only ever writes {role}*.py."
    if not _mock_dir(slug).exists():
        return (
            f"No mock folder for `{slug}` at {_mock_dir(slug)}. Use "
            "start_mock_attempt first, or check the id with list_mock_attempts."
        )
    if path.exists() and not overwrite:
        return (
            f"{path} already exists and overwrite was not set. Pass "
            "overwrite=True if you intend to replace it."
        )

    today = date.today().isoformat()
    parts = [f'"""\n{label} — `{slug}` (round {round_no}), written {today}.\n']
    if notes.strip():
        parts.append(f"\n{notes.strip()}\n")
    parts.append('"""\n\n')
    parts.append(code.strip() + "\n")
    path.write_text("".join(parts), encoding="utf-8")

    index = _load_index()
    for rec in reversed(index.get("lld_mock", [])):
        if rec["problem_id"] == slug and rec["round"] == round_no:
            rec[f"{role}_path"] = str(path.resolve())
            break
    _save_index(index)
    _regenerate_lld_feedback()

    attempt = _mock_file(slug, "attempt", round_no)
    return (
        f"{label} saved to {path}.\n"
        f"Compare against the attempt: {attempt}"
    )


@mcp.tool()
def save_ideal_solution(problem_id: str, code: str, notes: str = "", round_no: int = 0, overwrite: bool = False) -> str:
    """Write the interviewer's ideal solution for a mock LLD problem, as
    ideal.py inside that problem's "Mock Solutions/" folder — right next to
    the user's own attempt so the two can be read side by side.

    Call this after save_mock_evaluation. Write the design you'd expect from a
    strong candidate at the target level: complete classes, the patterns you
    flagged as missing in the evaluation, and a demo under
    `if __name__ == "__main__":`. Address the specific gaps you scored down.

    This is the thorough reference version — write it without worrying about
    how long it would take to produce live. For the cut-down version that
    actually fits an interview, follow up with save_simple_solution.

    This tool can only ever write a file named ideal*.py, so it cannot touch
    the user's attempt. It also refuses to replace an existing ideal solution
    unless overwrite=True.

    Args:
        problem_id: The mock problem's id (as used by start_mock_attempt).
        code: The full Python reference design, authored by you.
        notes: Optional prose added to the module docstring — why this
            structure, which patterns and what they buy, key trade-offs.
        round_no: Which round this is the ideal for. 0 (default) = latest.
        overwrite: Set True to replace an existing ideal solution.
    """
    return _save_mock_solution("ideal", "Ideal solution", problem_id, code, notes, round_no, overwrite)


@mcp.tool()
def save_simple_solution(problem_id: str, code: str, notes: str = "", round_no: int = 0, overwrite: bool = False) -> str:
    """Write a SIMPLER version of the ideal solution for a mock LLD problem, as
    simple.py in that problem's "Mock Solutions/" folder, alongside ideal.py
    and the user's attempt.

    ideal.py is the thorough reference design; simple.py is the same problem
    solved with the minimum that would still pass — the core classes and the
    one or two patterns that genuinely earn their place, with the extension
    points, secondary abstractions and exhaustive edge-case handling left out.
    It's the version a strong candidate could realistically finish inside the
    interview's time box, so the user has a target that's actually reachable
    under time pressure as well as one to aspire to.

    Call this after save_ideal_solution, on the same problem and round. It's
    optional — skip it when the ideal is already small enough that a pared-back
    version would be identical. The notes are the useful part: say explicitly
    what you dropped relative to the ideal and what it would cost.

    This tool can only ever write a file named simple*.py, so it cannot touch
    the user's attempt or the ideal. It refuses to replace an existing simple
    solution unless overwrite=True.

    Args:
        problem_id: The mock problem's id (as used by start_mock_attempt).
        code: The full pared-back Python design, authored by you.
        notes: Optional prose added to the module docstring — what was cut
            relative to ideal.py, and what that trade-off costs.
        round_no: Which round this is the simple version for. 0 (default) = latest.
        overwrite: Set True to replace an existing simple solution.
    """
    return _save_mock_solution("simple", "Simple solution", problem_id, code, notes, round_no, overwrite)


def _regenerate_lld_feedback() -> str:
    """Rewrite Mock Solutions/feedback.md from index["lld_mock"]. Derived
    entirely from the index, so it's always consistent with the tracked
    scores and safe to regenerate after every evaluation."""
    index = _load_index()
    records = index.get("lld_mock", [])
    if not records:
        return ""

    lines = [
        "# LLD Mock Interview Feedback",
        "",
        "_Auto-generated by the interview-memory MCP server after each "
        "evaluation. Regenerated in full every time — edit the per-problem "
        "evaluation.md files instead of this one._",
        "",
        "## Rubric averages (1-5, lower = focus here)",
        "",
    ]
    lines += _markdown_table(
        ["Dimension", "Average", "Times scored"],
        ((d, f"{avg:.1f}", n) for d, avg, n in _rated_dimensions()),
    )
    unrated = _unrated_dimensions()
    if unrated:
        lines += ["", f"Not yet exercised: {', '.join(unrated)}."]

    weakest = _weakest_dimensions()
    if weakest:
        lines += [
            "",
            "## Focus for the next mock",
            "",
            *(f"- **{d}** — averaging {avg:.1f}/5 over {n} attempt(s)" for d, avg, n in weakest),
            "",
            "Pick problems that force these dimensions, and push hardest on "
            "them during the interview.",
        ]

    lines += ["", "## Attempt history", ""]
    lines += _markdown_table(
        ["Date", "Problem", "Round", "Verdict", "Average", "Weakest this round"],
        _mock_history_rows(records),
    )

    LLD_FEEDBACK_MD.parent.mkdir(parents=True, exist_ok=True)
    LLD_FEEDBACK_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"Feedback file updated: {LLD_FEEDBACK_MD}."


@mcp.tool()
def get_lld_feedback() -> str:
    """Summarize how the user is performing across LLD mock interviews: rubric
    averages per dimension, the weakest dimensions, and recent attempt
    history with verdicts.

    Call this BEFORE suggest_next_problems("LLD") whenever running an LLD
    session. Use it to choose a problem that forces the weakest dimensions,
    and to decide what to push hardest on during the interview — that's how
    the loop actually tunes itself over time rather than just recording
    scores.
    """
    index = _load_index()
    records = index.get("lld_mock", [])
    if not records:
        return (
            "No LLD mock evaluations recorded yet. Use start_mock_attempt to "
            "pose a problem, then save_mock_evaluation once the user has "
            "written their attempt — the rubric averages build up from there."
        )

    lines = [
        f"{len(records)} LLD mock attempt(s) evaluated.",
        "",
        "Rubric averages (1-5):",
        *(f"- {d}: {avg:.1f} ({n} rated)" for d, avg, n in _rated_dimensions()),
    ]
    unrated = _unrated_dimensions()
    if unrated:
        lines.append(f"- not yet exercised: {', '.join(unrated)}")

    weakest = _weakest_dimensions()
    if weakest:
        lines += [
            "",
            "Weakest dimensions — bias the next problem toward these, and "
            "probe them hard during the interview:",
            *(f"- {d} ({avg:.1f}/5 over {n} attempt(s))" for d, avg, n in weakest),
        ]

    lines += ["", "Recent attempts:", ""]
    lines += _markdown_table(
        ["Date", "Problem", "Round", "Verdict", "Average", "Weakest this round"],
        _mock_history_rows(records, limit=10),
    )
    lines += ["", f"Full scorecard: {LLD_FEEDBACK_MD}"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tools: the LLD drill log (short focused reps, not full mock interviews)
# ---------------------------------------------------------------------------

@mcp.tool()
def log_lld_drill(
    topic: str,
    content_markdown: str,
    problem_id: str = "",
    duration_minutes: int = 0,
    gaps: str = "",
) -> str:
    """Append one LLD drill to the running drill log
    (LLD_SOLUTIONS_DIR/DRILL_LOG.md), creating the file if it doesn't exist
    yet. This is the persistence step of the LLD-drill skill.

    A drill is a short focused rep — one pattern, one class hierarchy, one
    "how would you extend this" question — not a full mock interview. Use
    start_mock_attempt / save_mock_evaluation for those; they get their own
    folder, rubric scores and per-problem files. A drill only ever appends
    here, so a session of six quick reps stays one readable file.

    Write the ENTIRE entry body yourself in content_markdown — this tool only
    persists it and stamps the dated header above it. Start any headings in
    the body at `###` and don't emit a bare `---` rule: `## ` and `---` are
    what separate one drill from the next in this file.

    Also updates the shared trackers: `gaps` feed the weak-area counts that
    get_progress_summary reports, and passing problem_id counts the drill as
    an attempt on that problem (so suggest_next_problems knows you've touched
    it). A doc previously linked by save_practice_doc is left alone.

    Args:
        topic: What was drilled, e.g. "Strategy vs. State for a vending
            machine" or "Design a Parking Lot".
        content_markdown: The full write-up of the drill in markdown, authored
            by you: what was asked, what the user produced, what to fix.
        problem_id: Optional LLD catalog id (see get_catalog) when the drill
            was on a cataloged problem, linking it into the problem tracker.
        duration_minutes: Optional length of the drill, stamped in the header.
        gaps: Optional semicolon-separated weak areas, same vocabulary as
            log_session — reuse consistent short names so they aggregate
            (e.g. "class-decomposition; interface segregation").
    """
    if not content_markdown.strip():
        return "content_markdown is empty — nothing to log."

    today = date.today().isoformat()
    index = _load_index()
    gap_list = _bump_weak_areas(index, gaps)
    _save_index(index)

    problem_note = ""
    if problem_id.strip():
        slug = _slugify(problem_id)
        entry = _record_practice("LLD", slug, None, today, title=topic)
        problem_note = f" Tracker updated for LLD `{slug}` (attempt #{entry['times_practiced']})."

    header = f"## {today} · {topic.strip()}"
    if duration_minutes > 0:
        header += f" · {duration_minutes} min"

    lines = [f"\n{LLD_DRILL_SEPARATOR}\n{header}", ""]
    if problem_id.strip():
        lines += [f"_Problem: `{_slugify(problem_id)}`_", ""]
    lines += [content_markdown.strip()]
    if gap_list:
        lines += ["", "**Gaps / to revise:** " + ", ".join(gap_list)]

    _ensure_drill_log()
    with LLD_DRILL_LOG_MD.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    return (
        f"Drill logged to {LLD_DRILL_LOG_MD} ({len(gap_list)} gap(s) recorded)."
        f"{problem_note}"
    )


@mcp.tool()
def get_lld_drill_log(limit: int = 5) -> str:
    """Read back the LLD drill log written by log_lld_drill. Call this at the
    start of a drill session to see what was drilled recently and what was
    left unresolved, so the next reps build on it instead of repeating it.

    Args:
        limit: How many of the most recent drills to return (newest last).
            Pass 0 for the whole file.
    """
    if not LLD_DRILL_LOG_MD.exists():
        return (
            f"No drill log yet at {LLD_DRILL_LOG_MD}. "
            "Use log_lld_drill after the first drill."
        )

    text = LLD_DRILL_LOG_MD.read_text(encoding="utf-8")
    # Entries are separated by the "---" rule log_lld_drill writes; the first
    # chunk is the file preamble, so it isn't counted as a drill.
    _preamble, *entries = text.split(LLD_DRILL_SEPARATOR)
    if not entries:
        return f"{LLD_DRILL_LOG_MD} exists but has no drills logged yet."
    shown = entries[-limit:] if limit > 0 else entries

    heading = f"{len(entries)} drill(s) logged in {LLD_DRILL_LOG_MD}"
    if len(shown) < len(entries):
        heading += f" — showing the most recent {len(shown)}"
    return heading + ".\n\n" + LLD_DRILL_SEPARATOR.join(shown).strip()


if __name__ == "__main__":
    mcp.run(transport="stdio")
