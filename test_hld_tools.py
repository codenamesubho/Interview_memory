"""
Hermetic tests for the HLD mock loop: the pre-committed reference, the
attempt, the diff and the seven-dimension evaluation.

Builds a synthetic HLD_SOLUTIONS_DIR in a temp directory and exercises every
HLD tool against it. Nothing outside the temp dirs is read or written, so this
is safe to run at any time -- in particular it never touches the real
HLD_SOLUTIONS_DIR the user keeps their designs in.

The acceptance checklist for this feature is the test plan, one named check
each: reference.md can't be overwritten, written_at comes from the server
clock, a diff without both sides errors, a free-form rubric key is rejected,
Mock Solutions/ never leaks into list_practice_docs, a doc with no
"## End-to-end flows" warns but still writes, and get_hld_feedback on an
empty history returns cleanly.

Usage:
  python3 test_hld_tools.py
"""

import importlib
import os
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

FAILS = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


FLOWS = """
## End-to-end flows

### Flow 1: job registration
1. API writes to `jobs` via `INSERT`.

### Flow 2: firing
1. Sweeper runs `SELECT ... FOR UPDATE SKIP LOCKED` on `job_runs`.

### Flow 3: worker crash
1. Lease expires; sweeper re-claims the run.
"""

REFERENCE = "## Invariants\n\n- exactly-once firing\n" + FLOWS
ATTEMPT = (
    "## Interviewer's structured rendering\n\n- polling sweeper\n"
    "\n## Arrived-at-only-after-prompting\n\n- idempotency keys, after "
    '"what happens if the worker dies mid-run?"\n'
)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="hld-tools-test-"))
    hld = tmp / "HLD"
    hld.mkdir(parents=True)

    # Point every root at the sandbox BEFORE importing server: the module reads
    # these at import time, so a real root would be created and written to.
    os.environ["INTERVIEW_PREP_DIR"] = str(tmp / "prep")
    os.environ["HLD_SOLUTIONS_DIR"] = str(hld)
    os.environ["LLD_SOLUTIONS_DIR"] = str(tmp / "LLD")
    os.environ["DSA_SOLUTIONS_DIR"] = str(tmp / "dsa")
    os.environ["BEHAVIORAL_SOLUTIONS_DIR"] = str(tmp / "behavioral")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    server = importlib.import_module("server")
    server = importlib.reload(server)
    check("test sandbox is isolated from the real HLD root",
          server.HLD_SOLUTIONS_DIR == hld, str(server.HLD_SOLUTIONS_DIR))

    today = date.today().isoformat()
    slug = "design-distributed-task-scheduler"

    # --- empty history -----------------------------------------------------
    print("\n-- get_hld_feedback on an empty history --")
    empty = server.get_hld_feedback()
    check("get_hld_feedback on empty history returns cleanly",
          "No HLD mock evaluations recorded yet" in empty, empty[:120])
    check("list_hld_mock_attempts before any attempt returns cleanly",
          "doesn't exist" in server.list_hld_mock_attempts())

    # --- start_hld_mock_attempt -------------------------------------------
    print("\n-- start_hld_mock_attempt --")
    started = server.start_hld_mock_attempt(
        problem_id=slug,
        title="Design a Distributed Task Scheduler",
        reference_markdown=REFERENCE,
        variant_of="design-cron",
        difficulty="Hard",
    )
    folder = hld / "Mock Solutions" / f"{today}-{slug}"
    reference = folder / "reference.md"
    check("attempt folder created with the dated name", folder.is_dir(), str(folder))
    check("folder path returned to the caller", str(folder) in started, started[:200])
    check("reference.md written", reference.exists())

    ref_text = reference.read_text()
    check("frontmatter records the problem id", f"problem_id: {slug}" in ref_text)
    check("frontmatter records frozen: true", "frozen: true" in ref_text)
    check("frontmatter records variant_of", "variant_of: design-cron" in ref_text)
    check("reference body preserved", "exactly-once firing" in ref_text)

    stamp = next((l[len("written_at: "):] for l in ref_text.splitlines()
                  if l.startswith("written_at: ")), "")
    parsed = None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        pass
    check("written_at parses as a timestamp", parsed is not None, stamp)
    # The server clock, not a tool argument -- there is no parameter that could
    # backdate it, and the value must agree with this machine's own clock.
    check("written_at comes from the server clock (today, not caller-supplied)",
          parsed is not None and parsed.date() == date.today(), stamp)
    check("no tool argument can set written_at",
          "written_at" not in server.start_hld_mock_attempt.__doc__)

    # --- refusing to overwrite a frozen reference --------------------------
    print("\n-- the pre-commitment is binding --")
    again = server.start_hld_mock_attempt(
        problem_id=slug, title="Design a Distributed Task Scheduler",
        reference_markdown="## Invariants\n\n- rewritten after the fact\n" + FLOWS,
    )
    check("second start_hld_mock_attempt on the same folder errors",
          "refusing to overwrite" in again.lower(), again[:160])
    check("the error names the existing folder", folder.name in again, again[:200])
    check("the error points at the -r2 escape hatch", "round_no=2" in again, again[:240])
    check("the frozen reference is untouched",
          "exactly-once firing" in reference.read_text()
          and "rewritten after the fact" not in reference.read_text())

    r2 = server.start_hld_mock_attempt(
        problem_id=slug, title="Design a Distributed Task Scheduler",
        reference_markdown=REFERENCE, round_no=2,
    )
    r2_folder = hld / "Mock Solutions" / f"{today}-{slug}-r2"
    check("round_no=2 opens a separate folder alongside", r2_folder.is_dir(), r2[:160])

    # --- the flows warning -------------------------------------------------
    print("\n-- the End-to-end flows warning --")
    no_flows = server.start_hld_mock_attempt(
        problem_id="design-rate-limiter", title="Design a Rate Limiter",
        reference_markdown="## Invariants\n\n- token bucket\n",
    )
    rl_folder = hld / "Mock Solutions" / f"{today}-design-rate-limiter"
    check("reference with no flows section still writes",
          (rl_folder / "reference.md").exists())
    check("reference with no flows section warns",
          "End-to-end flows" in no_flows and "WARNING" in no_flows, no_flows[:200])
    check("reference WITH a flows section does not warn", "WARNING" not in started, started[:200])

    # --- path guards -------------------------------------------------------
    print("\n-- path guards --")
    escape = server.save_hld_attempt("../../../etc", ATTEMPT, raw_turns="x")
    check("a traversing attempt_folder is refused", "must name a folder inside" in escape, escape[:120])
    check("an absolute path outside the mock dir is refused",
          "must name a folder inside" in server.save_hld_attempt(str(tmp), ATTEMPT), escape[:120])
    check("the mock root itself is not an attempt folder",
          server._resolve_hld_folder(str(hld / "Mock Solutions")) is None)
    check("a folder name inside the mock dir resolves",
          server._resolve_hld_folder(folder.name) == folder.resolve())

    # --- diff before attempt errors ---------------------------------------
    print("\n-- save_hld_diff needs both sides --")
    early = server.save_hld_diff(folder.name, "a", "b", "c", "flow diff")
    check("save_hld_diff with a missing attempt.md errors",
          "No attempt.md" in early, early[:160])
    check("no diff.md written when a side is missing", not (folder / "diff.md").exists())

    # --- save_hld_attempt --------------------------------------------------
    print("\n-- save_hld_attempt --")
    saved = server.save_hld_attempt(
        folder.name, ATTEMPT,
        raw_turns="so I'd have a table of jobs, and, um, a poller that reads it",
    )
    attempt = folder / "attempt.md"
    text = attempt.read_text()
    check("attempt.md written", attempt.exists(), saved[:120])
    check("verbatim turns kept under 'As stated by the candidate'",
          "## As stated by the candidate" in text and "um, a poller" in text)
    check("structured rendering section present",
          "## Interviewer's structured rendering" in text)
    check("arrived-at-only-after-prompting section present",
          "## Arrived-at-only-after-prompting" in text)
    check("no warning when raw_turns and both sections are supplied",
          "WARNING" not in saved, saved[:200])

    bare = server.save_hld_attempt(r2_folder.name, "## Some notes\n\njust prose\n")
    check("missing raw_turns warns that the diff grades a reconstruction",
          "raw_turns was empty" in bare, bare[:240])
    check("missing arrived-at-only-after-prompting section warns",
          "Arrived-at-only-after-prompting" in bare, bare[:300])
    check("the attempt still writes despite the warnings",
          (r2_folder / "attempt.md").exists())

    # --- save_hld_diff -----------------------------------------------------
    print("\n-- save_hld_diff --")
    diffed = server.save_hld_diff(
        folder.name,
        matched="job store; sweeper",
        missed="exactly-once firing",
        diverged="polling instead of a timer wheel",
        diff_markdown="### Flow-level diff\n\nReference fires via lease; attempt polls.\n",
    )
    diff_md = (folder / "diff.md").read_text()
    check("diff.md written", (folder / "diff.md").exists(), diffed[:120])
    check("missed invariants listed as a real gap",
          "## Missed invariants" in diff_md and "exactly-once firing" in diff_md)
    check("diverged kept separate from missed",
          "## Diverged at choice points" in diff_md and "timer wheel" in diff_md)
    check("bucket counts reported back", "1 matched" not in diffed and "2 matched" in diffed, diffed[:160])
    check("a diff that never mentions flows warns",
          "WARNING" in server.save_hld_diff(r2_folder.name, "a", "b", "c", "component table only"))
    check("the good diff.md is left intact", "timer wheel" in (folder / "diff.md").read_text())

    # --- save_hld_evaluation ----------------------------------------------
    print("\n-- save_hld_evaluation --")
    good_scores = {"requirements": 4, "capacity-estimation": 3, "architecture": 4,
                   "deep-dives": 3, "scale-calibration": 4, "communication": 3,
                   "composure": 2}
    bad_key = server.save_hld_evaluation(
        folder.name, "Hire", "Hire@Senior", {"requirements": 4, "vibes": 5},
        "s", "g", "body",
    )
    check("a rubric key outside the fixed seven is rejected",
          "Unknown rubric dimension" in bad_key and "vibes" in bad_key, bad_key[:160])
    check("nothing written when the rubric key is rejected",
          not (folder / "evaluation.md").exists())
    check("an LLD rubric key is rejected on the HLD scorecard",
          "Unknown rubric dimension" in server.save_hld_evaluation(
              folder.name, "Hire", "x", {"class-decomposition": 4}, "s", "g", "b"))
    check("an out-of-range score is rejected",
          "Scores must be integers" in server.save_hld_evaluation(
              folder.name, "Hire", "x", {"requirements": 9}, "s", "g", "b"))
    check("an invalid verdict is rejected",
          "Invalid verdict" in server.save_hld_evaluation(
              folder.name, "Maybe", "x", good_scores, "s", "g", "b"))

    evaluated = server.save_hld_evaluation(
        attempt_folder=folder.name,
        verdict="Lean Hire",
        level_verdict="Hire@Senior, No-hire@Staff",
        rubric_scores=good_scores,
        strengths="clear structure; drove the session",
        gaps="capacity-estimation; composure",
        evaluation_markdown="Numbers were asserted, not derived.",
        persona="adversarial",
    )
    eval_md = (folder / "evaluation.md").read_text()
    check("evaluation.md written", (folder / "evaluation.md").exists(), evaluated[:160])
    check("verdict at level recorded", "Hire@Senior, No-hire@Staff" in eval_md)
    check("persona recorded", "adversarial" in eval_md)
    check("scorecard rendered in fixed rubric order",
          eval_md.index("requirements") < eval_md.index("composure"))
    check("session logged (no separate log_session needed)",
          "Logged session" in evaluated, evaluated[:240])
    revision = (tmp / "prep" / "revision.md").read_text()
    check("session appended to revision.md", "[HLD] Design a Distributed Task Scheduler" in revision)

    # --- aggregation -------------------------------------------------------
    print("\n-- aggregation --")
    feedback = server.get_hld_feedback()
    check("get_hld_feedback reports the attempt", "1 HLD mock attempt(s) evaluated" in feedback, feedback[:120])
    check("rubric averages reported", "composure: 2.0" in feedback, feedback[:400])
    check("weakest dimension surfaced first",
          "composure (2.0/5" in feedback, feedback[:600])
    check("verdict at level surfaced", "Hire@Senior, No-hire@Staff" in feedback)
    scores = server._load_index()["competency_scores"]
    check("scores stored under the hld: prefix", "hld:composure" in scores, str(list(scores)[:5]))
    check("HLD scores don't pollute the LLD rubric namespace",
          not any(k.startswith("lld:") for k in scores), str(list(scores)))
    check("HLD rubric slice reads back un-prefixed",
          "composure" in server._rubric_scores(server.HLD_RUBRIC_PREFIX))
    check("LLD feedback unaffected by HLD scores",
          "No LLD mock evaluations recorded yet" in server.get_lld_feedback())

    summary = server.get_progress_summary()
    check("progress summary reports the HLD rubric in its own block",
          "HLD mock rubric" in summary, summary[-600:])
    check("HLD scores are not reported as behavioral competencies",
          "Competency scores (behavioral" not in summary, summary[-600:])
    check("progress summary points at get_hld_feedback",
          "Call get_hld_feedback before an HLD session" in summary, summary[-400:])

    # --- listing -----------------------------------------------------------
    print("\n-- list_hld_mock_attempts --")
    listing = server.list_hld_mock_attempts()
    check("graded attempt shows its verdict", "Lean Hire" in listing, listing[:400])
    check("ungraded attempt shown as not graded", "not graded" in listing)
    check("incomplete attempts called out",
          "Incomplete:" in listing and f"{today}-design-rate-limiter" in listing, listing[:800])
    filtered = server.list_hld_mock_attempts(problem_id="design-rate-limiter")
    check("filtering by problem id keeps the matching attempt",
          f"`{today}-design-rate-limiter`" in filtered, filtered[:400])
    check("filtering by problem id excludes other problems",
          f"`{today}-{slug}`" not in filtered, filtered[:400])
    check("unknown problem id reports cleanly",
          "No HLD mock attempts for" in server.list_hld_mock_attempts(problem_id="nope"))

    # --- read_hld_mock_file ------------------------------------------------
    print("\n-- read_hld_mock_file --")
    check("reference.md readable in full",
          "exactly-once firing" in server.read_hld_mock_file(folder.name, "reference.md"))
    check("a filename outside the four is refused",
          "filename must be one of" in server.read_hld_mock_file(folder.name, "../../../etc/passwd"))
    check("a missing file reports cleanly",
          "No diff.md in" in server.read_hld_mock_file(rl_folder.name, "diff.md"))

    # --- practice docs stay separate from the evidence ---------------------
    print("\n-- Mock Solutions/ vs. list_practice_docs --")
    doc = server.save_practice_doc(
        "HLD", "Design a Distributed Task Scheduler",
        "## Requirements\n\n- fire jobs on time\n" + FLOWS, problem_id=slug,
    )
    check("practice doc saved to the HLD root, not the mock folder",
          str(hld / f"{slug}.md") in doc, doc[:200])
    docs = server.list_practice_docs("HLD")
    check("list_practice_docs surfaces the practice doc", f"`{slug}`" in docs, docs[:400])
    check("list_practice_docs surfaces nothing under Mock Solutions/",
          "Mock Solutions" not in docs, docs[:600])

    no_flow_doc = server.save_practice_doc("HLD", "Design a Rate Limiter", "## Requirements\n\n- limit\n")
    check("HLD doc without flows still writes",
          (hld / "design-a-rate-limiter.md").exists())
    check("HLD doc without flows warns in the tool result",
          "WARNING" in no_flow_doc and "End-to-end flows" in no_flow_doc, no_flow_doc[:240])
    check("HLD doc with flows does not warn", "WARNING" not in doc, doc[:200])
    lld_doc = server.save_practice_doc("LLD", "Design a Parking Lot", "## Classes\n\n- Lot\n")
    check("the flows warning is HLD-only (no false alarm on LLD)",
          "WARNING" not in lld_doc, lld_doc[:200])

    print()
    if FAILS:
        print(f"{len(FAILS)} failure(s): " + ", ".join(FAILS))
        return 1
    print(f"All checks passed. (sandbox: {tmp})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
