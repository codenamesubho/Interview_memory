# Interview Practice MCP Server

A local MCP server that gives Claude persistent memory of your DSA / HLD / LLD / Behavioral interview practice — backed by plain files on your disk that you can open and review anytime.

It knows a built-in catalog of ~230 problems (149 DSA, 30 HLD, 25 LLD, 29 Behavioral), tracks what you've attempted and when, and can recommend what to practice next: new problems, or ones due for revision, biased toward your flagged weak areas.

## What it stores

Session/tracking data, under `~/interview-prep/` by default:

- **`revision.md`** — human-readable chronological log, one section per session. This is the file *you* review.
- **`index.json`** — compact machine index: sessions, aggregated weak areas, competency scores (Behavioral), and a per-problem tracker (times practiced, last verdict, last attempt date, absolute path to the linked solution/doc). Keeps Claude's context small.
- **`custom_catalog.json`** — problems you've added yourself (e.g. from a mock interview), merged with the built-in `catalog.json` shipped next to `server.py`.

**Per-problem solutions/docs live in four independent, separately configurable directories** — each defaults to a folder under `~/interview-prep/docs/` but is normally pointed at wherever your real practice work lives:

- **`DSA_SOLUTIONS_DIR`** (e.g. `~/code/DSA_mock`) — organized as `<Topic_Folder>/<problem>.py`, one real Python file per problem grouped by topic, matching a typical personal LeetCode-practice layout. DSA is code, not markdown — see `save_dsa_solution` below.
- **`HLD_SOLUTIONS_DIR`** (e.g. `~/code/HLD`) — one markdown file per HLD problem (e.g. `design-rate-limiter.md`).
- **`LLD_SOLUTIONS_DIR`** (e.g. `~/code/LLD`) — holds two separate things: the **reference corpus** of your own designs, one self-contained `.py` per problem grouped into category folders (`1_state_machine/vending_machine.py`), plus flat `design-parking-lot.md` write-ups from `save_practice_doc`; and **`Mock Solutions/`**, the mock-interview loop where you attempt a problem and Claude grades it. See [LLD mock interviews](#lld-mock-interviews) below. A third file, **`DRILL_LOG.md`**, sits at the top level: one running, append-only log of short LLD drills (see [LLD drills](#lld-drills)).
- **`BEHAVIORAL_SOLUTIONS_DIR`** (e.g. `~/code/Behavioral`) — holds a single `candidate_context.md`: your reusable background + STAR story bank, with a "Current Focus" section you refresh per company/role. See `save_candidate_context` below.

## Tools exposed to Claude

| Tool | When Claude uses it |
|---|---|
| `get_progress_summary` | Start of every session — session table, weak areas, competency scores, catalog coverage per type |
| `get_catalog` | Browse the full problem pool for DSA/HLD/LLD/Behavioral, filterable by topic/difficulty/status, to find a problem's exact id |
| `suggest_next_problems` | "What should I practice next?" — ranks due-for-revision and weak-area-matching problems above fresh new ones |
| `add_custom_problem` | Add a problem not in the built-in catalog (company-specific, from a mock interview, a real behavioral question you were asked, etc.) |
| `log_session` | End of every session, all types — appends to `revision.md`, updates weak areas and the per-problem tracker; optionally records per-competency scores (1-5) and improvement suggestions |
| `save_practice_doc` | HLD/LLD only — after a design discussion, writes its own markdown file for quick revision |
| `get_practice_doc` | Fetch one saved HLD/LLD doc in full, e.g. to revise a specific past solution |
| `list_practice_docs` | List all saved HLD/LLD docs (title, topic, last updated) as a revision menu |
| `save_candidate_context` | Behavioral only — persist your reusable candidate profile + STAR story bank as one markdown file, overwriting the previous version |
| `get_candidate_context` | Fetch the saved candidate context in full, e.g. at the start of a behavioral mock session |
| `scan_dsa_directory` | Read `DSA_SOLUTIONS_DIR` — folder/filename/problem-statement-snippet/last-modified for every `.py` file, flagging which are already tracked |
| `import_solved_dsa_problem(s)` | Backfill the tracker from an existing file on disk (matched to a catalog id) *without* touching the file — uses its mtime as "last practiced" |
| `save_dsa_solution` | After solving a NEW DSA problem — writes a `.py` file into the right topic folder in `DSA_SOLUTIONS_DIR`; refuses to overwrite an existing file unless told to |
| `scan_lld_directory` | Read `LLD_SOLUTIONS_DIR` — folder/filename/**kind**/snippet/last-modified for every file in the reference corpus, flagging which are already tracked. Only `kind=solution` rows are per-problem designs; `Mock Solutions/` is excluded |
| `read_lld_solution` | Open ONE LLD file in full — a past design, a corpus doc, or a mock attempt you're about to grade |
| `import_solved_lld_problem(s)` | Backfill the tracker from an LLD design already on disk, *without* touching the file. Refuses anything that isn't `kind=solution` |
| `save_lld_solution` | Write a NEW design into the reference corpus, in the right category folder; refuses to overwrite unless told to |
| `start_mock_attempt` | Pose an LLD problem — creates `Mock Solutions/<id>/` with `problem.md` and an `attempt.py` stub for *you* to fill in. Never overwrites an existing attempt; re-posing opens the next round |
| `list_mock_attempts` | Every mock problem and round: which files exist, what's still awaiting grading, scores and verdicts |
| `save_mock_evaluation` | Grade an attempt against the fixed 7-dimension rubric — writes `evaluation.md`, logs the session, and regenerates `feedback.md` |
| `save_ideal_solution` | Write the interviewer's reference design as `ideal.py`, beside your attempt. Can only ever write `ideal*.py`, so it cannot touch your own file |
| `save_simple_solution` | Write a pared-back version of that design as `simple.py` — what a strong candidate could realistically finish in the time box. Can only ever write `simple*.py` |
| `get_lld_feedback` | Rubric averages, weakest dimensions, and attempt history — call this *before* `suggest_next_problems("LLD")` to aim the next question |
| `log_lld_drill` | Append one short LLD drill to `DRILL_LOG.md` — a quick rep that doesn't warrant a whole mock folder. Feeds the same weak-area tracker as `log_session` |
| `get_lld_drill_log` | Read back the last N drills — call at the start of a drill session so the next reps build on the last ones |
| `get_current_time` | Read this machine's wall clock (local, UTC, epoch) — to time a rep (call at both ends, pass the delta as `duration_minutes`) or to check today's date instead of assuming it |
| `get_session_detail` | Revisit the full log entry for one past session |
| `resolve_weak_area` | Remove a weak area once you've demonstrably improved at it |

## Setup (macOS)

1. Keep this folder wherever you cloned it, e.g. `/Users/YOUR_USERNAME/code/file_mcp/`. `server.py` and `catalog.json` must stay next to each other — the catalog is loaded relative to the script.

2. Install the MCP SDK (any Python ≥3.10):

   ```bash
   pip3 install mcp
   ```

   (If you use `uv`: `uv pip install mcp`, and adjust the command below to your venv's python.)

3. Open Claude Desktop → **Settings → Developer → Edit Config**. This opens `~/Library/Application Support/Claude/claude_desktop_config.json`. Add:

   ```json
   {
     "mcpServers": {
       "interview-memory": {
         "command": "python3",
         "args": ["/Users/YOUR_USERNAME/code/file_mcp/server.py"],
         "env": {
           "INTERVIEW_PREP_DIR": "/Users/YOUR_USERNAME/interview-prep",
           "DSA_SOLUTIONS_DIR": "/Users/YOUR_USERNAME/code/DSA_mock",
           "HLD_SOLUTIONS_DIR": "/Users/YOUR_USERNAME/code/HLD",
           "LLD_SOLUTIONS_DIR": "/Users/YOUR_USERNAME/code/LLD",
           "BEHAVIORAL_SOLUTIONS_DIR": "/Users/YOUR_USERNAME/code/Behavioral"
         }
       }
     }
   }
   ```

   Use absolute paths. If `python3` on your PATH isn't the one with `mcp` installed, use the full path (check with `which python3`). Omit any of the four `*_SOLUTIONS_DIR` vars you don't need — each independently falls back to a folder under `INTERVIEW_PREP_DIR`.

4. Fully quit Claude Desktop (Cmd+Q) and reopen it. You should see the server's tools under the connectors/tools indicator in the chat input.

**Note:** `~/code/HLD` only ever receives what this server writes, so there's nothing to import there. `~/code/LLD` is different — it already holds a real corpus of hand-written `.py` designs in category folders, so start with the [first-time LLD import](#first-time-lld-import) below. Older LLD work outside `LLD_SOLUTIONS_DIR` (e.g. `~/code/Practice LLD/chess/main.py`) is not reachable by these tools; move it under `~/code/LLD` if you want it tracked.

## First-time DSA import

If `DSA_SOLUTIONS_DIR` already has solutions in it (like an existing `Arrays_and_Hashing/`, `Graphs/`, etc. layout), the tracker starts out blank until you import — the files existing on disk isn't the same as the MCP knowing about them. Ask Claude:

> Scan my DSA directory and import everything you can confidently match to the catalog.

Claude will call `scan_dsa_directory()`, read each file's problem-statement snippet, match filenames like `island.py` → `number-of-islands` or `top_k.py` → `top-k-frequent-elements` using its own judgment (filenames rarely match catalog ids exactly), and call `import_solved_dsa_problems` in bulk. Anything it can't confidently match (scratch files, company-specific problems) it should flag for you to decide — add those via `add_custom_problem` first if you want them tracked. This step only reads files and writes to `index.json`; it never modifies anything under `DSA_SOLUTIONS_DIR`.

## First-time LLD import

`LLD_SOLUTIONS_DIR` (`~/code/LLD`) already holds ~31 hand-written designs in numbered category folders, none of which the tracker knows about. Ask Claude:

> Scan my LLD directory and import everything you can confidently match to the catalog.

`scan_lld_directory()` returns every file with a **Kind** column — `solution` (a per-problem design), `category-doc` (a folder README), `aggregate-doc` (`INDEX.md`, `QUICK_REFERENCE.md` and friends, which span many problems), `practice-doc` (a markdown write-up this server wrote via `save_practice_doc`), `drill-log` (`DRILL_LOG.md`), or `other`. Only `solution` rows are importable; `import_solved_lld_problem(s)` refuses the rest, so an index file can never get linked to a single problem. Claude matches filenames to catalog ids by judgment (`8_lru_cache.py` → `design-lru-cache-oop`), and roughly half the corpus isn't in the 25-entry built-in LLD catalog at all (`order_lifecycle.py`, `whatsapp_messaging.py`, `audit_trail.py`, …) — those need `add_custom_problem` first. Nothing under `~/code/LLD` is modified; only `index.json` is written.

## LLD mock interviews

The loop that turns practice into targeted practice. You write the design yourself; Claude grades it as the interviewer and remembers where you're weak.

```
~/code/LLD/Mock Solutions/
  feedback.md                    # running scorecard, regenerated after each grading
  design-parking-lot/
    problem.md                   # the prompt, as Claude posed it
    attempt.py                   # YOUR work — no tool in this server ever writes over it
    evaluation.md                # Claude's scored critique
    ideal.py                     # Claude's reference design
    simple.py                    # the same design pared back to interview scope
```

Re-attempting a problem later opens the next round (`attempt_2.py`, `evaluation_2.md`, `ideal_2.py`, `simple_2.py`), so you keep the earlier one to diff against.

**The flow.** Say:

> Let's do an LLD mock interview.

1. Claude calls `get_lld_feedback()` and `suggest_next_problems("LLD")` to pick a problem aimed at your weakest dimensions, then `start_mock_attempt(...)` — which writes the prompt and an empty `attempt.py`.
2. You write your design in `attempt.py`, on your own. Say when you're done.
3. Claude calls `read_lld_solution("Mock Solutions/design-parking-lot/attempt.py")`, then `save_mock_evaluation(...)` with rubric scores and a written critique, then `save_ideal_solution(...)` and optionally `save_simple_solution(...)`.
4. Read `evaluation.md` and diff `attempt.py` against `ideal.py` — and against `simple.py` for the version that actually fits the clock.

**The rubric** is a fixed seven-dimension vocabulary, scored 1-5. It's fixed on purpose: free-form labels would never aggregate, and the aggregate is exactly what makes later sessions pick different problems.

| Dimension | What it measures |
|---|---|
| `requirements-and-scope` | Clarifying questions, scoping, stated assumptions |
| `class-decomposition` | Entity/responsibility split, cohesion |
| `design-patterns` | Pattern choice, and whether it was justified |
| `solid-and-extensibility` | SOLID, open/closed, how the design absorbs change |
| `concurrency-and-edge-cases` | Thread safety, races, boundary conditions |
| `code-quality` | Naming, structure, idiomatic Python |
| `tradeoff-communication` | Articulating and defending alternatives |

Scores accumulate in `index.json` under `lld:`-prefixed keys (kept apart from Behavioral competency scores, which share that store), and surface in three places: `feedback.md` for you to read, `get_lld_feedback()` and `get_progress_summary()` for Claude, and a weakest-dimensions note appended to `suggest_next_problems("LLD")`. That last one matters — the generic weak-area ranking matches against catalog *topics*, and every LLD topic starts with "OOP Design", so without the rubric the feedback would never actually change what gets asked.

**Safety.** `attempt.py` is yours: `start_mock_attempt` refuses to overwrite one, `save_ideal_solution` can only ever write a file named `ideal*.py`, and `save_simple_solution` only `simple*.py`. Scans and imports exclude `Mock Solutions/` entirely, so Claude's own output can never be backfilled into the tracker as your finished work.

## LLD drills

A drill is a short focused rep — one pattern, one class hierarchy, one "how would you extend this" — not a full mock interview. Drills don't get a folder, a prompt file or rubric scores; they all append to a single running log:

```
~/code/LLD/DRILL_LOG.md      # newest drill at the bottom, entries split by ---
```

Claude calls `get_lld_drill_log()` at the start of a drill session to see what you covered last time, and `log_lld_drill(...)` after each rep. The signature:

```
log_lld_drill(topic, content_markdown, problem_id="", duration_minutes=0, gaps="")
get_lld_drill_log(limit=5)          # limit=0 returns the whole file
```

`duration_minutes` doesn't have to be guessed: Claude can call `get_current_time()` when you start the rep and again when you finish, and log the difference.

`content_markdown` is the whole entry body, written by Claude — start its headings at `###` and don't put a bare `---` rule inside it, since `##` and `---` are what separate one drill from the next. `gaps` is a semicolon-separated list in the same vocabulary as `log_session`, and feeds the same weak-area counts that `get_progress_summary` reports. Passing `problem_id` also counts the drill as an attempt on that problem in the tracker — including refreshing its "last practiced" date, so a drill postpones that problem's next revision — but never unlinks a doc `save_practice_doc` wrote for it.

## Wiring it into your practice routine

Say this at the start of a chat (or bake it into a Claude Desktop project/skill):

> At the start of every practice session, call `get_progress_summary`, then `suggest_next_problems` for the type we're practicing (DSA/HLD/LLD) and let me pick from the top few. For LLD also call `get_lld_feedback` first and aim the pick at my weakest rubric dimensions. After we solve/discuss it, call `log_session` (with `problem_id` set). Then for HLD/LLD call `save_practice_doc` with the full write-up (HLD: requirements, capacity estimate, architecture, API/data model, trade-offs; LLD: class design, patterns used, key decisions). For DSA, if the problem isn't already on disk, call `save_dsa_solution` with the final code and a short explanation.
>
> If I say I want to *attempt* an LLD problem myself rather than discuss it, run the mock loop instead: `start_mock_attempt`, wait for me to write `attempt.py`, then `read_lld_solution`, `save_mock_evaluation` (score honestly — inflated scores break the feedback loop), `save_ideal_solution`, and `save_simple_solution` for the cut-down version.

Example flow (HLD):

1. **"What should I practice today?"** → Claude calls `get_progress_summary`, then `suggest_next_problems("HLD")`, and proposes 2-3 options (mixing anything due for revision with new ones matching your weak areas).
2. You work through the problem together.
3. Claude calls `log_session(..., problem_id="design-rate-limiter")` to record the verdict/gaps, then `save_practice_doc("HLD", "Design a Rate Limiter", <full writeup>, problem_id="design-rate-limiter")`, creating `HLD_SOLUTIONS_DIR/design-rate-limiter.md`.
4. Next time you ask to revise, `list_practice_docs()` shows everything documented so far, and `get_practice_doc` pulls up any specific one in full.

Example flow (DSA):

1. `suggest_next_problems("DSA")` → you pick "Course Schedule".
2. You solve it together.
3. `log_session(..., problem_id="course-schedule")`, then `save_dsa_solution("course-schedule", "Course Schedule", "Graphs", <code>, explanation="topological sort via Kahn's algorithm")` → creates `DSA_SOLUTIONS_DIR/Graphs/course_schedule.py`.

Example flow (Behavioral):

1. Before your first mock session, tell Claude about your background and stories; it calls `save_candidate_context(...)` to write `BEHAVIORAL_SOLUTIONS_DIR/candidate_context.md` (a "Core Profile & Story Bank" section, plus a "Current Focus" section for whichever company/role you're currently prepping for).
2. **"Let's do a behavioral mock for CompanyX."** → Claude calls `get_candidate_context()` to see your stories, then `suggest_next_problems("Behavioral")` and proposes a few questions (built-in catalog of 29 common questions across competencies like Leadership, Conflict & Disagreement, Ownership — see `catalog.json`), or you can just ask about a real question you were asked and it'll use `add_custom_problem("Behavioral", ...)` to track it.
3. You answer with a STAR story together.
4. Claude calls `log_session(..., interview_type="Behavioral", problem_id=..., competency_scores={"leadership": 4, "communication": 3}, improvements="Add a concrete metric to the result step.")` — this appends the verdict, scores, and improvement suggestions to `revision.md`, and updates the per-competency score averages shown in `get_progress_summary`.
5. Prepping for a different company next week? Call `save_candidate_context(...)` again with an updated "Current Focus" section — the Core Profile & Story Bank stays intact since you write the whole file each time.

## Tuning revision timing

By default a problem is considered "due for revision" 21 days after its last attempt (or immediately, regardless of days, if it matches a currently flagged weak area). Change it via the `INTERVIEW_PREP_STALE_DAYS` env var in the Claude Desktop config's `env` block.

## Adding more problems

`catalog.json` (next to `server.py`) is the shared, built-in catalog — feel free to hand-edit it, or just ask Claude to call `add_custom_problem`, which writes to `custom_catalog.json` inside your `INTERVIEW_PREP_DIR` instead (keeps your personal additions separate from the shipped list).

## Testing / autostart

A stdio-transport MCP server (what this is) isn't a background daemon you start once and leave running — Claude Desktop spawns it fresh as a subprocess every time it needs it, using whatever `command`/`args`/`env` is in `mcpServers.interview-memory` in `claude_desktop_config.json`. So there's no separate "autostart" toggle: **being registered in that config *is* autostart** — Desktop manages the process lifecycle automatically from then on. That's already wired up (see Setup above); you just need to fully quit (Cmd+Q, not just close the window) and reopen Claude Desktop for it to pick up the config.

Four ways to check it's actually working, in increasing order of realism:

1. **Does it even boot?** `python3 server.py` from this folder — should start and hang silently (that's correct; it's waiting for a client on stdin). Ctrl+C to stop.
2. **Does it speak MCP correctly?** `python3 test_server.py` — spawns the server as a real MCP client would, does the protocol handshake, lists all 29 tools, and calls `get_progress_summary` for real. Read-only, safe to run anytime. A clean "All checks passed" means the server itself is solid, independent of Claude Desktop.
3. **Do the LLD tools actually behave?** `python3 test_lld_tools.py` — builds a synthetic design repo in a temp directory and exercises every LLD tool against it: path guards, kind classification, rubric validation, the full mock loop, and above all that `attempt.py` comes out byte-identical to what was written. Hermetic — it never touches `~/code/LLD` or your real `index.json`.
4. **Is Claude Desktop actually using it?**
   - Open a chat and look at the tools/connectors icon near the input box — `interview-memory` should be listed with its tool count.
   - `ps aux | grep server.py` — while Desktop is open, you should see a live `python3 .../server.py` process (Desktop spawns it once you open a chat that uses it, or at startup depending on version).
   - Logs: `~/Library/Logs/Claude/mcp-server-interview-memory.log`.
   - Interactive debugging (optional): `mcp dev server.py` launches the MCP Inspector, a local web UI where you can call any tool by hand and see raw responses — useful for poking at edge cases outside a real chat.

## Security

This server only ever runs over **stdio** (`mcp.run(transport="stdio")`), the same as the original. Stdio means Claude Desktop spawns it as a local subprocess and talks to it over stdin/stdout pipes — it opens no network port, so there is nothing for another machine, process, or website to connect to. Don't change the transport to `sse`/`http` unless you also add authentication and bind to `localhost`; as shipped, "outside access" isn't a meaningful attack surface because there's no listener at all.

Within the local filesystem, a few guardrails are worth knowing about since Claude drives these tools somewhat autonomously:

- **Path sanitization.** Every value used to build a filename (`problem_id`, problem titles) is passed through a slugifier that strips everything except `a-z 0-9 -` before it ever touches a path. A value like `../../etc/passwd` becomes `etc-passwd`, not a traversal — verified with an adversarial test during development.
- **Directory containment.** `save_practice_doc` can only write inside `HLD_SOLUTIONS_DIR` or `LLD_SOLUTIONS_DIR` (whichever matches the call); `save_dsa_solution` can only write inside `DSA_SOLUTIONS_DIR`; `save_candidate_context`/`get_candidate_context` are confined to `BEHAVIORAL_SOLUTIONS_DIR`; `import_solved_dsa_problem(s)` can only *link* to files that already live inside `DSA_SOLUTIONS_DIR` (it refuses anything outside it, e.g. `~/.ssh/`, `/etc/`); `get_practice_doc` re-checks that whatever `index.json` points at is still inside the allowed directory for that problem type before reading it.
- **No code execution.** The server only reads and writes text files. It never `exec`s, `eval`s, or runs the DSA solutions it stores — `code`/`content_markdown` arguments are treated purely as bytes to persist.
- **Overwrite protection.** `save_dsa_solution` refuses to replace an existing file unless `overwrite=True` is passed explicitly, since `DSA_SOLUTIONS_DIR` is assumed to hold real, hand-written work. (`save_practice_doc` for HLD/LLD, and `save_candidate_context` for Behavioral, do overwrite by design — they hold Claude's latest write-up/profile, and those directories are managed entirely by this server.)
- **Env-var-scoped roots.** `INTERVIEW_PREP_DIR`, `DSA_SOLUTIONS_DIR`, `HLD_SOLUTIONS_DIR`, `LLD_SOLUTIONS_DIR`, and `BEHAVIORAL_SOLUTIONS_DIR` are only ever set by you, in your own Claude Desktop config — they aren't something a chat message can override.

## Troubleshooting

- Server not appearing: check JSON syntax in the Claude Desktop config (one bad comma disables everything), confirm paths are absolute, then look at logs in `~/Library/Logs/Claude/mcp-server-interview-memory.log`.
- Test the server manually: `python3 server.py` should start and wait silently (Ctrl+C to exit).
- `ModuleNotFoundError: No module named 'mcp'`: run `pip3 install mcp` with the same `python3` referenced in the config.
- `save_dsa_solution` refusing to write: it never overwrites an existing file without `overwrite=True` — this is deliberate, since `DSA_SOLUTIONS_DIR` is treated as containing real, hand-written solutions.
