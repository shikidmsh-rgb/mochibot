# MochiBot — Claude Code Instructions

## Project Identity
- Open-source version of the private "Mochi" project
- Features come from Mochi; MochiBot does NOT independently expand features
- **Upstream source**: Private Mochi project lives at `M:\mochi` (same machine). When you need to reference the original implementation, read from there.

## Open Source Rules (Critical)
1. **No personal info** — remove all personal identifiers from code, comments, configs
2. **New-user ready** — every feature must work for any user out of the box
3. **Config-driven** — no hardcoded models, API providers, or credentials; all configurable via `.env` or config files
4. **Based on Mochi** — features originate from Mochi, MochiBot does not diverge

## Workflow

Every non-trivial change follows this pipeline. Do NOT skip or reorder phases.

### Phase 1: Understand
- Listen to the user's requirement. Ask clarifying questions with `AskUserQuestion` until the goal is unambiguous.
- **When in doubt, ASK. Do NOT guess.** If you are unsure about scope, intended behavior, edge cases, priority, or anything else — ask the user before proceeding. Making assumptions and building the wrong thing wastes everyone's time. It is always better to ask one question too many than to silently guess wrong.

### Phase 2: Discuss
- Discuss direction and trade-offs with the user in plain conversation. No code yet.

### Phase 3: Plan + Review
- Enter plan mode with `EnterPlanMode`. Read `ARCHITECTURE.md` first.
- Your plan document MUST have two parts:

  **Part 1 — Product (for PM / non-technical reader):**
  Write this section assuming the reader has zero coding background.
  - What does this feature/change do, in plain language?
  - User experience BEFORE vs AFTER — describe what the user will see/feel/do differently.
  - Any new config the user needs to set up?
  - Edge cases or limitations the user should know about.

  **Part 2 — Technical (architecture & implementation):**
  - Approach and rationale.
  - Affected files (create / modify / delete).
  - Architecture compliance, open-source fitness, risks (see **Plan Review Checklist** below).

- After writing the plan, spawn a review agent (`.claude/agents/review.md`) via the Agent tool to review your plan from an **architect's perspective**. Include the review result in the plan document.
- Exit plan mode with `ExitPlanMode` for user approval. Answer any questions the user has.

### Phase 4: Implement
- Write code following the architecture and conventions.
- If you hit something unexpected during implementation, **stop and ask** — do not silently deviate from the approved plan.

### Phase 5: Test
- **Unit tests:** Run `pytest`. Add/update tests for new behavior. Don't proceed until they pass.
- **Feature E2E simulation:** For any user-facing feature, go to `M:\mochitest` and simulate realistic usage scenarios (happy path, edge cases, error conditions). This is not optional — pytest alone tests code correctness, E2E tests feature correctness.
- **Doc updates:** Check and update any affected documentation:
  - `ARCHITECTURE.md` if layers/structure changed
  - `SKILL.md` if a skill was added/modified
  - `docs/SKILL_SPEC.md` if skill conventions changed
  - `.env.example` if new config was added
  - `README.md` if user-facing behavior changed

### Phase 6: Report
- Summarize what was done in plain language (PM-friendly, no code dumps).
- List: what changed, what was tested, what docs were updated.
- **E2E test cases:** List every scenario that was simulated on mochitest — what input was given, what was expected, what actually happened (pass/fail).
- Flag anything that needs the user's attention or follow-up.

Small fixes (typos, single-line changes) can skip phases 2-3 but still need phase 6.

### Plan Review Checklist
Every plan (Phase 3) MUST address each item. Mark "N/A" with a reason if not applicable.

- **Open Source Fitness**: No personal info? Generic for all users? All values from `.env`/config?
- **Architecture Compliance**: Follows 5-layer architecture? One-way dependency flow? Skills self-contained? Observers read-only? Business logic not in transport?
- **Solution Quality**: Simplest viable approach? No over-engineering? No under-engineering?
- **Bug Fix Quality** (if applicable): Root cause identified? Regression test planned?
- **Anti-Whack-a-Mole**: No band-aid prompting (adding "禁止XXX" to fix one incident — fix the prompt structure or data flow instead)? No LLM capability clipping (if/else or keyword lists that replace judgments the LLM should make)?
- **Security & Privacy**: No secrets in code? Input validation at boundaries? `.env.example` updated if new config needed?
- **Affected Files**: List every file that will be created/modified/deleted.
- **Doc Updates Needed**: Which docs need updating?
- **Risks**: What could go wrong? Migration concerns? Breaking changes?

## No Guesswork, No Band-Aids
- **Read before you write.** Always read the actual source code and logs before suggesting a fix. Never guess what the code "probably" does.
- **Fix root causes, not symptoms.** Patching around a bug (adding try/except to silence errors, wrapping with defensive checks that mask the real problem) is forbidden. Trace the issue to its true origin and fix it there.
- **No speculative fixes.** If you don't understand why something is broken, say so and investigate further — don't throw code at the wall to see what sticks.

## Architecture First
- **Always read `ARCHITECTURE.md` before proposing structural changes.**
- Respect the layer architecture (L1–L5) and one-way dependency direction.
- New skills go in `mochi/skills/{name}/`, new observers in `mochi/observers/{name}/`.
- Follow the Key Rules at the bottom of `ARCHITECTURE.md` — especially: transport = dumb pipe, skills are self-contained, observers are read-only.
- If a proposed change would violate the architecture, flag it and suggest an alternative.

## Skill Development Rules
When creating or modifying skills, these rules are non-negotiable:

### Isolation
- **Never modify `db.py` for skill-specific logic** — tables go in `init_schema()`, queries go in `queries.py` within the skill directory
- **Never import other skills** — skills are self-contained; no cross-skill function calls or imports
- **Never import upward** — `skill → heartbeat`, `skill → ai_client`, `skill → transport` are all forbidden. Skills may only import from `mochi.skills.base`, `mochi.db`, `mochi.config`, `mochi.llm`, and standard library
- **No cross-skill foreign keys** — each skill owns its own DB tables exclusively

### DB Autonomy
- **`init_schema(conn)`** — declare all tables with `CREATE TABLE IF NOT EXISTS`. No destructive DDL. Framework calls `conn.commit()`
- **`queries.py`** — all DB read/write functions live here. Import `_connect` from `mochi.db`
- **`ensure_column(conn, table, col, typedef)`** — use for adding columns to existing tables (schema migrations)
- **handler.py imports from `queries.py`**, not from `db.py`

### SKILL.md
- Required fields: `name`, `description` (pre-router classification depends on description)
- Tool format: `## Tools` → `### tool_name (L0/L1/L2)` → parameter table

### Testing
- Every new skill needs tests in `tests/test_{name}_handler.py`
- Test fixtures (`fresh_db`) automatically provide fresh DB + skill schemas — no manual DB setup needed
- If adding a new `fresh_db` fixture in a test class, always call `skill_registry.init_all_skill_schemas()` after `init_db()`

### Reference
- Full spec: `docs/SKILL_SPEC.md`
- Starter template: `docs/skill_template/`

## Commit Rule
- **Every commit must also sync to `M:\mochitest`** — that directory is a git worktree of this repo on the `mochitest` branch. After committing on `main`, sync by running:
  ```
  cd /m/mochitest && git merge main --ff-only
  ```
- **User data is safe** — `.env`, `data/`, `.venv/` are in `.gitignore`, so the merge never touches mochitest's own API keys or database. Do NOT manually copy these files.
- If `--ff-only` fails (mochitest branch diverged), reset it: `cd /m/mochitest && git reset --hard main`

## Build & Run
- Python project, uses `pip` / `venv`
- Database: SQLite (in `data/`)
- Tests: `pytest`

## Code Conventions
- Follow existing code style in the repo
- Keep dependencies minimal
- All config via `.env` — see `.env.example` for reference

## Communication Style
- End responses with a kaomoji (｡•̀ᴗ-)✧
