"""
Hermetic tests for the LLD corpus tools and the mock-interview loop.

Builds a synthetic LLD_SOLUTIONS_DIR in a temp directory (mirroring the shape
of a real design repo: numbered category folders, corpus-wide docs, junk) and
exercises every LLD tool against it. Nothing outside the temp dirs is read or
written, so this is safe to run at any time.

Unlike test_server.py -- which only checks that the server speaks MCP and
exposes the expected tool names -- this checks what the tools actually do:
path guards, kind classification, rubric validation, and above all that the
user's own attempt.py is never modified.

Usage:
  python3 test_lld_tools.py
"""

import hashlib
import importlib
import os
import sys
import tempfile
from pathlib import Path

FAILS = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_corpus(root: Path) -> None:
    """A miniature of a real LLD repo: category folders with designs and
    READMEs, corpus-wide docs at the top, and an extensionless junk file."""
    for folder, files in {
        "1_state_machine": ["1_vending_machine.py", "elevator_system.py", "README.md"],
        "4_game_design": ["chess_game.py", "snake_and_ladder.py"],
        "Splitwise": ["splitwise.py", "Splitwise.txt"],
    }.items():
        (root / folder).mkdir(parents=True)
        for name in files:
            (root / folder / name).write_text(f'"""\n{name}\n"""\n\n\nclass Demo:\n    pass\n')
    (root / "INDEX.md").write_text("# Index of all designs\n")
    (root / "QUICK_REFERENCE.md").write_text("# Quick reference\n")
    (root / "LLD").write_text('"""\nLow level Design\n"""\n')  # extensionless junk


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="lld-tools-test-"))
    lld = tmp / "LLD"
    build_corpus(lld)

    os.environ["INTERVIEW_PREP_DIR"] = str(tmp / "prep")
    os.environ["LLD_SOLUTIONS_DIR"] = str(lld)
    os.environ["DSA_SOLUTIONS_DIR"] = str(tmp / "dsa")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    server = importlib.import_module("server")

    mock = lld / "Mock Solutions"

    # --- kind classification ----------------------------------------------
    out = server.scan_lld_directory()
    kinds = {}
    for line in out.splitlines():
        if line.startswith("| ") and not line.startswith("| Folder"):
            c = [x.strip() for x in line.split("|")[1:-1]]
            kinds.setdefault(c[2], []).append(f"{c[0]}/{c[1]}")

    check("designs classified as solution", len(kinds.get("solution", [])) == 5,
          str(sorted(kinds.get("solution", []))))
    check("corpus-wide docs are aggregate-doc",
          {"(top level)/INDEX.md", "(top level)/QUICK_REFERENCE.md"} <= set(kinds.get("aggregate-doc", [])))
    check("folder README is category-doc", "1_state_machine/README.md" in kinds.get("category-doc", []))
    check("extensionless junk is other", "(top level)/LLD" in kinds.get("other", []))
    check("category filter works",
          "chess_game.py" in server.scan_lld_directory("game")
          and "elevator_system.py" not in server.scan_lld_directory("game"))

    # save_practice_doc's own write-ups must not look like corpus-wide indexes
    server.save_practice_doc("LLD", "Design a Toy Cache", "Entities: Cache, Node.", problem_id="design-toy-cache")
    check("practice docs distinguished from aggregate docs",
          "| practice-doc |" in server.scan_lld_directory(), "")

    # --- read guards -------------------------------------------------------
    check("reads a design", "class Demo" in server.read_lld_solution("4_game_design/chess_game.py"))
    for bad in ["../../etc/passwd", "/etc/passwd", str(Path("~/.ssh").expanduser())]:
        r = server.read_lld_solution(bad)
        check(f"refuses to read {bad!r}",
              r.startswith("Refusing to read outside") or r.startswith("File not found"), r[:80])
    check("missing file reported", server.read_lld_solution("nope.py").startswith("File not found"))
    check("directory reported", "is a directory" in server.read_lld_solution("4_game_design"))

    # --- import guards -----------------------------------------------------
    check("refuses importing an aggregate doc",
          "not `solution`" in server.import_solved_lld_problem("design-chess-game", "INDEX.md"))
    check("refuses importing outside the root",
          "Refusing to import" in server.import_solved_lld_problem("design-chess-game", "/etc/passwd"))
    r = server.import_solved_lld_problem("design-chess-game", "4_game_design/chess_game.py")
    check("imports a real design", r.startswith("Imported `design-chess-game`"), r[:80])
    entry = server._load_index()["problems"]["LLD"]["design-chess-game"]
    check("catalog metadata wins over caller's", entry["title"] == "Design a Chess Game", entry["title"])
    check("scan marks it imported", "| yes |" in server.scan_lld_directory("game"))

    # --- empty-state messages ---------------------------------------------
    check("list_mock_attempts handles no mock dir", "doesn't exist" in server.list_mock_attempts())
    check("get_lld_feedback handles no evaluations", "No LLD mock evaluations" in server.get_lld_feedback())

    # --- the mock loop -----------------------------------------------------
    r = server.start_mock_attempt("design-parking-lot", "Design a Parking Lot", "Handle multiple floors.")
    attempt = mock / "design-parking-lot" / "attempt.py"
    check("round 1 started", "Round 1" in r, r[:80])
    check("problem.md written",
          (mock / "design-parking-lot" / "problem.md").exists()
          and "multiple floors" in (mock / "design-parking-lot" / "problem.md").read_text())
    check("attempt stub written", attempt.exists())

    # the user writes their own design
    user_code = '"""My own attempt."""\n\n\nclass ParkingLot:\n    def park(self, v):\n        return None\n'
    attempt.write_text(user_code)
    before = sha(attempt)

    check("re-posing opens round 2, never clobbers",
          "Round 2" in server.start_mock_attempt("design-parking-lot", "Design a Parking Lot", "again"))
    check("attempt untouched by re-posing", sha(attempt) == before)
    check("round 2 files suffixed", (mock / "design-parking-lot" / "attempt_2.py").exists())
    (mock / "design-parking-lot" / "attempt_2.py").unlink()
    (mock / "design-parking-lot" / "problem_2.md").unlink()

    body = server.read_lld_solution("Mock Solutions/design-parking-lot/attempt.py")
    check("reads attempt via a path containing a space", "class ParkingLot" in body, body[:80])
    check("attempt classified as mock-attempt", "kind: mock-attempt" in body)

    # validation happens before anything is written
    for label, args in [
        ("unknown rubric key", ({"made-up": 3}, "Hire")),
        ("out-of-range score", ({"code-quality": 9}, "Hire")),
        ("bad verdict", ({"code-quality": 3}, "Maybe")),
        ("empty rubric", ({}, "Hire")),
    ]:
        scores, verdict = args
        r = server.save_mock_evaluation("design-parking-lot", scores, verdict, "a", "b", "x")
        check(f"rejects {label}", r.startswith(("Unknown rubric", "Invalid", "rubric_scores is empty")), r[:80])
    check("rejects grading a problem with no attempt",
          "No attempt found" in server.save_mock_evaluation("never-posed", {"code-quality": 3}, "Hire", "a", "b", "x"))
    check("attempt untouched by rejected calls", sha(attempt) == before)

    r = server.save_mock_evaluation(
        "design-parking-lot",
        {"class-decomposition": 2, "design-patterns": 3, "code-quality": 4, "concurrency-and-edge-cases": 1},
        "Lean Hire",
        "clear naming",
        "class-decomposition; concurrency-and-edge-cases",
        "The design collapses Vehicle and Spot into one class.",
        title="Design a Parking Lot",
    )
    evaluation = mock / "design-parking-lot" / "evaluation.md"
    check("evaluation written", evaluation.exists())
    check("average reported", "2.5/5" in r, r[:120])
    check("score table follows rubric order",
          evaluation.read_text().index("class-decomposition") < evaluation.read_text().index("code-quality"))
    check("session logged", "Logged session 1" in r, r[:160])
    check("attempt untouched by grading", sha(attempt) == before)

    ftext = (mock / "feedback.md").read_text()
    check("feedback ranks weakest first",
          ftext.index("concurrency-and-edge-cases") < ftext.index("code-quality"))
    check("feedback notes unexercised dimensions", "Not yet exercised" in ftext)
    check("feedback records history", "Design a Parking Lot" in ftext and "Lean Hire" in ftext)
    check("get_lld_feedback names the weakest",
          "concurrency-and-edge-cases (1.0/5" in server.get_lld_feedback())

    index = server._load_index()
    check("rubric scores namespaced under lld:",
          all(k.startswith("lld:") for k in index["competency_scores"]),
          str(list(index["competency_scores"])))
    check("tracker points at the evaluation",
          index["problems"]["LLD"]["design-parking-lot"]["doc_path"].endswith("evaluation.md"))
    check("get_practice_doc pulls the evaluation",
          "evaluation" in server.get_practice_doc("LLD", "design-parking-lot"))

    # behavioral competencies must stay in their own section
    server.log_session("A conflict story", "Behavioral", "Hire", "clear", "metrics",
                       competency_scores={"leadership": 4})
    summary = server.get_progress_summary()
    check("summary separates behavioral from LLD rubric",
          "Competency scores (behavioral" in summary and "LLD mock rubric" in summary)
    check("behavioral section excludes lld: keys", "lld:" not in summary.split("LLD mock rubric")[0])
    check("LLD suggestions carry the weak-dimension note",
          "Weakest LLD rubric dimensions" in server.suggest_next_problems("LLD"))
    check("DSA suggestions do not", "Weakest LLD rubric" not in server.suggest_next_problems("DSA"))

    # --- ideal solution ----------------------------------------------------
    ideal = mock / "design-parking-lot" / "ideal.py"
    server.save_ideal_solution("design-parking-lot", "class Spot:\n    pass\n", notes="Composite + Strategy.")
    check("ideal written", ideal.exists() and "class Spot" in ideal.read_text())
    check("ideal carries notes", "Composite + Strategy." in ideal.read_text())
    check("ATTEMPT STILL BYTE-IDENTICAL", sha(attempt) == before)
    check("attempt is exactly what the user wrote", attempt.read_text() == user_code)
    check("refuses to overwrite ideal",
          "overwrite was not set" in server.save_ideal_solution("design-parking-lot", "class Other:\n    pass\n"))
    check("ideal unchanged after refusal", "class Spot" in ideal.read_text())
    server.save_ideal_solution("design-parking-lot", "class Other:\n    pass\n", overwrite=True)
    check("overwrite=True replaces it", "class Other" in ideal.read_text())
    check("refuses ideal with no mock folder",
          "No mock folder" in server.save_ideal_solution("never-posed", "x = 1"))

    # --- listing -----------------------------------------------------------
    listing = server.list_mock_attempts()
    check("lists the graded round",
          "`design-parking-lot`" in listing and "Lean Hire" in listing and "2.5/5" in listing)
    check("nothing awaiting evaluation yet", "Awaiting evaluation" not in listing, listing[-120:])
    server.start_mock_attempt("design-chess-game", "Design a Chess Game", "Full rules.")
    check("ungraded attempts flagged", "Awaiting evaluation" in server.list_mock_attempts())

    # --- reference-corpus writes ------------------------------------------
    r = server.save_lld_solution("design-tic-tac-toe", "Design Tic Tac Toe", "game design",
                                 "class Board:\n    pass\n", explanation="Strategy pattern.")
    check("fuzzy category resolves to 4_game_design",
          (lld / "4_game_design" / "design_tic_tac_toe.py").exists(), r[:120])
    check("refuses to overwrite an existing design",
          "overwrite was not set" in server.save_lld_solution(
              "design-tic-tac-toe", "Design Tic Tac Toe", "4_game_design", "class B:\n    pass\n"))
    check("bare category name matches a numbered folder",
          (lld / "1_state_machine" / "design_a_vending_machine.py").exists()
          or server.save_lld_solution("design-vending-machine", "Design a Vending Machine",
                                      "state machine", "class VM:\n    pass\n") is not None)
    check("vending machine landed in 1_state_machine",
          (lld / "1_state_machine" / "design_a_vending_machine.py").exists())

    # Claude's own mock output must never re-enter the corpus
    check("corpus scan excludes Mock Solutions", "Mock Solutions" not in server.scan_lld_directory())
    check("refuses to import an ideal solution",
          "not `solution`" in server.import_solved_lld_problem(
              "design-parking-lot", "Mock Solutions/design-parking-lot/ideal.py"))
    check("refuses to import an attempt",
          "not `solution`" in server.import_solved_lld_problem(
              "design-parking-lot", "Mock Solutions/design-parking-lot/attempt.py"))

    print()
    if FAILS:
        print(f"{len(FAILS)} failure(s): " + ", ".join(FAILS))
        return 1
    print(f"All checks passed. (sandbox: {tmp})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
