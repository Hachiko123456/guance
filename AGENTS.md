# Guance Project Coordination

## Project

Guance is a Windows 10/11 desktop application for local Daguandan state capture, safe state reconstruction, and FableDan recommendations. This repository is a new implementation; do not copy the legacy GUI wholesale.

## Ownership for this implementation batch

- Lead: contracts, model integration (`src/guance/infrastructure/model/**`), project composition, public docs, pyproject, integration, packaging, GitHub push.
- Core worker: `src/guance/domain/**`, `src/guance/infrastructure/protocol/**`, `tests/domain/**`, `tests/protocol/**`.
- Runtime worker: `src/guance/infrastructure/capture/**`, `src/guance/infrastructure/windows/**`, `src/guance/application/runtime/**`, `src/guance/ports/capture.py`, `tests/runtime/**`.
- GUI worker: `src/guance/presentation/**`, `src/guance/presentation/qml/**`, `tests/presentation/**`.

Do not edit another worker's owned paths. Keep business logic out of QML and platform/process code out of the domain layer.

## Safety and product boundaries

- No automatic game clicking or play execution.
- Recommendations are suggestions; only confirmed observed actions mutate state.
- Never silently fall back from the required model in production mode.
- Never log or export cookies, authorization headers, CA private keys, or hidden opponent hands.
- Network capture is for the user's own authorized environment; do not add request replay or traffic modification.

## Validation

Use Python 3.12 x64. Run focused tests first, then the relevant pytest layers. Keep the application importable on non-Windows hosts where possible; Windows-only behavior must have explicit skips/markers.

<!-- GUANCE-CODEX-WORKFLOW:BEGIN -->
## Codex single-main-session development workflow

This project authorizes the current main user conversation to use native Codex subagents on demand for later development requests. The user's constraints in the current request always take priority. Do not use another main session, `codex exec`, a fork, a cloud task, a new project/repository, a branch switch, or a worktree for this workflow.

### Routing and role boundaries

- Pure consultation, explanation, or design discussion does not modify code.
- If the request is ambiguous, call `analyst` for read-only requirements, boundary, compatibility, impact, and evidence analysis.
- If the request crosses module interfaces, data structures, dependencies, or key technical choices, call `architect` for read-only minimal design and task decomposition.
- Send authorized implementation to `implementer`; send independent test design/execution or test-only additions to `test_engineer`; send independent diff and risk review to `reviewer`.
- Use only the roles justified by complexity and risk; do not force every small task through all five roles. Split large work into independently acceptable tasks.
- The five project roles are `.codex/agents/analyst.toml`, `architect.toml`, `implementer.toml`, `test_engineer.toml`, and `reviewer.toml`. They are on-demand roles, not resident agents.

### Acceptance ownership and automatic routing

- Before delegating implementation for any functional change or bug fix, the main agent must write or update a task-specific acceptance mapping from the original request. Reuse an existing task acceptance record or an acceptance section in `docs/agent/STATE.md`; link it from that state file and include it in each task packet. Start from `docs/ACCEPTANCE_STANDARD.md`, select affected P0/P1/P2 IDs, and add task-local IDs for new behavior not covered by the existing standard.
- For every acceptance item, record the source requirement, prerequisites/input, observable expected result, test or manual verification method, required evidence, and blocking priority. Mark checks not yet run as pending; after execution distinguish passed, failed, skipped, and unverified. Criteria must exist before code is written, not be reverse-engineered from the implementation.
- `analyst` derives requirement-to-acceptance conditions when the request is ambiguous; `architect` supplies design and interface acceptance conditions when the request crosses boundaries; `test_engineer` designs and executes the independent test matrix and returns evidence; `reviewer` checks acceptance coverage and risks.
- The main agent is the final owner and writer of `docs/ACCEPTANCE_STANDARD.md`, `docs/ACCEPTANCE.md`, and task-specific acceptance records. Child agents may provide drafts, tests, matrices, and evidence, but they do not make the release decision or replace the main agent's final document update.
- Architecture review is internal by default: the main agent evaluates an architect's report when that role is needed and records the chosen design. Clear tasks that reuse the existing architecture do not require an architect or a user-confirmation gate. Continue within the authorized scope; ask the user only for major unresolved ambiguity, irreversible operations, external side effects, or a decision explicitly requiring user choice.
- Reuse established acceptance documents rather than creating duplicate standards on reruns. Main-agent standard/record edits obey the same single-writer handoff as code and tests. Do not weaken criteria to accommodate a failing implementation; record any requirement-driven criteria change and rerun affected checks.
- A development request is not complete until the main agent checks the actual candidate diff, runs the required affected validations, and records acceptance results including passed, failed, skipped, and unverified items.

### Delegation packet and lifecycle

Every delegation must state: task ID, original request, objective, non-goals, prerequisites, allowed files/paths, interface constraints, acceptance conditions, and required evidence. The child must report changed files, exact commands, working directory, exit codes, result summary, skipped/unverified checks, environment limits, and remaining risks.

- At most 3 child threads may be open at once, excluding the main conversation.
- Read-only investigations may run in parallel. At most one workspace writer may operate at a time, whether that writer is the main agent or a child; the main agent pauses its own writes while child write ownership is active.
- The main agent explicitly grants and revokes write ownership. Implementation, test additions, and main-agent documentation edits all obey the single-writer rule.
- A child must not create, message, or otherwise delegate to another child. Project config sets V1 `agents.max_depth = 1`; role instructions separately prohibit recursion. If the active backend ignores this V1 setting, the prompt prohibition is not a system-level guarantee and must be reported.
- Route fixes for the same task back to the original child when practical; create a new child only for a genuinely independent task. Close children when no longer needed.
- Do not accept a child summary as proof of completion. The main agent checks actual tracked, untracked, and changed files, maps the result to the original request, and runs final candidate-state validation. Pause relevant writes during acceptance; rerun affected checks after any later change.
- After three unsuccessful repair rounds without substantive progress, re-diagnose, re-split, or replace the same-model instance; then mark the issue blocked rather than retrying indefinitely.

### Write and safety controls

- A child may write only within the paths explicitly authorized in its task packet and must preserve unrelated user modifications.
- `implementer` may change production code and directly related unit tests only when granted write ownership. `test_engineer` may change test or test-helper paths only when granted write ownership and may never change production code. `analyst`, `architect`, and `reviewer` are read-only.
- Never weaken or delete tests, bypass checks, silently downgrade required acceptance, add automatic game clicking/play execution, replay or traffic modification, or log/export cookies, authorization headers, CA private keys, or hidden opponent hands.
- Keep business logic out of QML and platform/process code out of the domain layer. Follow the ownership map above.

### Acceptance, state, and recovery

The main agent owns final acceptance. Use `docs/agent/CONTEXT.md` for verified project entry points and commands, and `docs/agent/STATE.md` for the current task, writer, dependencies, evidence, blockers, and next step. Do not invent commands; mark unrun checks as pending. Reconcile the records with the actual tree after context compression or recovery before repeating work.

The project configuration is `.codex/config.toml`. It requests `gpt-6-astra` with `ultra` for the main agent, the `[agents]` defaults, and all five role TOMLs. If the model is changed later, update all six files: `.codex/config.toml` plus the five role TOMLs. Within `.codex/config.toml`, update the top-level model/reasoning fields and both `[agents]` defaults; within every role TOML, update its model/reasoning fields. Do not change user-level authentication, MCP, provider, notification, skills, or permission settings for this workflow.
<!-- GUANCE-CODEX-WORKFLOW:END -->
