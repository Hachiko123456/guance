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
