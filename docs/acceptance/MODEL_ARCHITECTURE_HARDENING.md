# MODEL-ARCHITECTURE-HARDENING-20261005 — pre-implementation acceptance mapping

## Scope and ownership

- Original request: second-stage architecture hardening for Guance, a Daguandan-only application that must support multiple model providers/runtimes within the Guandan domain; preserve FableDan compatibility; keep Guandan production explicitly unavailable without a real checkpoint; harden typed contracts, dependency direction, lifecycle/stale protection, worker/manifest compatibility, UI/config/package boundaries, local-capture privacy acceptance, and documentation.
- Repository: `D:\project\guance`; related training project `D:\project\guandan` is read-only.
- Date/timezone: 2026-10-05, Asia/Shanghai.
- Protected baseline: branch `codex/auto-training-data-demo` with pre-existing dirty model/training-data/UI changes. Baseline snapshot: `build/acceptance/model-architecture-hardening-20261005/`.
- Main agent owns this record, `docs/agent/STATE.md`, final diff review, final acceptance, and release decision. No commit/push/worktree/branch switch is authorized.
- Capture boundary: do not modify, copy, move, print, upload, or track `debug/captures`; no payload, full game, hidden hand, credentials, model weights, or request replay may enter evidence or Git.

## Acceptance criteria

Each item is defined before implementation. Status values are `pending`, `passed`, `failed`, `skipped`, or `unverified`; a partial result must be split or remain pending/unverified rather than called passed.

| ID | Source requirement / priority | Prerequisites and input | Observable expected result | Verification method and required evidence | Blocker | Status | Unverified reason |
|---|---|---|---|---|---|---|---|
| ARCH-01 | User §§1, 8, 18; P0-009/P1-005 | Guandan-only composition plus at least two provider descriptors | No multi-game registry; adding provider changes registration/provider only, not Controller/service flow | AST/import review, architecture tests, focused tests, diff; dependency graph evidence | P0 | unverified | Static provider registry/boundary tests pass; no independent add-provider end-to-end proof. |
| ARCH-02 | User §9; P0-008/P0-009 | Domain/application imports and composition root | Domain is Qt/process/model-free; application uses contracts/ports; concrete infrastructure is assembled at composition root | AST boundary tests and import smoke; exact pytest output | P0 | passed | Architecture and domain/protocol boundary tests pass; parser/runtime/model composition is injected. |
| ARCH-03 | User §10; P0-008/P1-002 | Typed DTOs and production controller/model path | Typed request/status/result/capability objects are used in production; no new callable-dict dual contract; wire dicts stop at codec boundary | Contract tests, type/AST inspection, focused pytest output | P0 | passed | Typed DTO/status/result and strict canonical decoding tests pass. |
| ARCH-04 | User §§11, 16; P0-006/P0-008 | Domain observation, canonical public view, provider input, wire envelope | Public model input is a privacy-approved projection; no hidden opponent hand/future/post-state/settlement/reward/payload | Privacy allowlist tests, serialized-payload tests, static review; no payload output | P0 | passed | Synthetic privacy/contract tests pass; real capture canonical coverage remains separately unverified. |
| ARCH-05 | User §12; P1-004 | ModelService/client/worker/provider objects | Ownership and state transitions for unselected/unavailable/loading/ready/error/closed are explicit; repeat start/close/import behavior deterministic | Lifecycle transition tests and source review | P0 | passed | Lifecycle and ownership tests pass. |
| ARCH-06 | User §12; P0-008/P1-002 | Concurrent submit, sync callback, invalidate/reload/close, same state key | Immutable identity includes generation/epoch, request/session/game/state; stale callbacks and same-key old requests are rejected | Deterministic race tests with barriers/callback injection; exact test evidence | P0 | passed | Request identity, synchronous callback, invalidate/reload/close and missing-ID stale regression tests pass. |
| ARCH-07 | User §13; P0-009/P2-004 | Manifest, artifact, optional SHA256SUMS, separate schema versions | Strict manifest/path containment/artifact and checksum validation; incompatible versions/capabilities reject; no arbitrary import/command | Manifest/path/hash negative tests and diff review | P0 | passed | Manifest/path/artifact/SHA256SUMS/version negative tests pass; clean package build remains unverified. |
| ARCH-08 | User §13; P0-009/P1-010 | Missing provider metadata, legacy AdviceResult/NPZ | Compatibility is explicit at legacy entry points; no guessed provider or unconditional FableDan default; failed import never restores prior model | Legacy decoder/adapter tests and import review | P0 | passed | Explicit legacy decoder/no-provider guessing/no-fallback tests pass. |
| ARCH-09 | User §§1, 14; P1-005 | Existing FableDan `.npz`, no torch/onnx import environment | Existing advisor/NPZ remains compatible; all candidates validated semantically; default startup is FableDan-compatible without torch | Existing and new FableDan tests, import smoke, source review | P1 | unverified | Synthetic FableDan/no-heavy-runtime tests pass; real packaged/private model inference remains unverified. |
| ARCH-10 | User §§1, 6, 7, 14; P0-009 | No real Guandan checkpoint/runtime; test-only fake runtime | Guandan production path is explicitly unavailable; fake runtime is test-only and never default-selected; no forged complete state | Provider registry/service tests, negative production selection tests, package scan | P0 | passed | Guandan unavailable boundary and test-only fake runtime tests pass. |
| ARCH-11 | User §§10, 14; P1-006 | FableDan, fake, and unavailable provider outputs | AdviceCandidate separates cards/action semantics/label/rank/score/score_kind/metadata; claim is optional/provider-labelled; legal action validation covers every candidate | DTO/provider tests and UI projection review | P1 | passed | Provider output structural/action-shape validation, candidate rank and claim separation tests pass; full wildcard/lead-beating semantics remain provider-specific. |
| ARCH-12 | User §13; P0-008 | Client and worker JSONL protocol | Client/worker share independent codec; no private-function import; protocol/message byte limits, malformed input and version mismatch fail closed | Codec tests, import-boundary test, malformed/oversize tests | P0 | passed | Shared codec malformed/oversized/duplicate/version/identity tests pass. |
| ARCH-13 | User §15; P0-006/P1-007 | ViewModel metadata/status/error projections | UI displays generic provider/backend/runtime/version/capabilities/fingerprint/status and safe error; does not interpret raw score/claim as FableDan semantics | Presentation tests, QML/source review, safe metadata test | P1 | passed | Model metadata projection, provider claim semantics, UI acceptance and presentation tests pass; Windows native visual/DPI remains unverified. |
| ARCH-14 | User §15; P0-009/P1-010 | Explicit source, missing/incompatible source, preferences | Selected/loading/loaded sources remain coherent; no silent alternate source/fallback; unavailable is not ready; legacy discovery isolated | Config/service/controller tests and source review | P0 | passed | Explicit source/no-fallback/config/composition tests pass. |
| ARCH-15 | User §16; P0-008/P1-001 | Five exact fixed capture selectors | Hash immutability, selector/legacy conversion/session quality, state monotonicity, settlement vs closed evidence, per-decision privacy/canonical/provider/fake checks; missing source explicit failure/unverified | `py -3.12 -B -m pytest -m local_capture -q`, script, safe summary only, no payload output | P0 | unverified | Marker conversion/privacy tests pass, but the explicit script intentionally exits 2 because canonical decision coverage and capture-closed sidecar evidence are incomplete. |
| ARCH-16 | User §§15, 16, 20; P0-006/P2-004/P2-006 | Packaging/spec/build tree and Git state | Explicit bundle allowlist; no capture/payload/credential/weight/test runtime; default package has no torch/Guandan heavy runtime | Static package tests/isolated build if safe, `git ls-files`, archive contents, diff review | P0 | unverified | Allowlist/hash/spec validation compiles and static checks pass; clean PyInstaller archive and Windows package execution were not run. |
| ARCH-17 | User §§3, 19; P0-010 | Python 3.12 x64 and current candidate tree | Focused → relevant → full tests run on actual candidate; failures/skips/unverified listed without weakening tests | Exact commands, exit codes, counts, compileall, diff check | P0 | passed | Full suite: 447 passed, 1 skipped, 5 deselected; relevant layer: 355 passed; acceptance/UI: 29 passed. |
| ARCH-18 | User §§17, 22; P0-009/P2-005 | Source/docs/tests/final evidence | Docs distinguish scaffold/unavailable/ready/real inference/model strength; no claim exceeds evidence; final report lists complete files/risks and release boundaries | Final reviewer report, doc/source/test consistency review | P0 | unverified | Acceptance/state records are updated, but final reviewer-3 failed with HTTP 503/auth unavailable; review-2 remains the last completed independent review. |

## Planned implementation sequence

1. Stage 0 audit and baseline (in progress; historical “passed” claims are not evidence).
2. Main records architecture decision and then grants one writer for core contracts/composition.
3. Core writer hardens typed contracts, dependency direction, lifecycle and protocol/compatibility without touching captures.
4. Provider/config/UI/package owners are handled serially by ownership paths.
5. Main freezes production code; test engineer receives test-only write ownership for independent negative/race/privacy/capture tests.
6. Main runs final candidate validations, reviews actual tracked/untracked diff, and updates every status with passed/failed/skipped/unverified evidence.

## Initial baseline evidence

- `py -3.12 -c "import sys, struct; ..."`: exit 0, Python 3.12 x64 confirmed.
- `py -3.12 -m pytest tests/model -q`: exit 0, 190 passed (2026-10-05 baseline).
- No child report is accepted yet: two requested native read-only agents ended with 429 capacity errors; a retry may be attempted once with the same requested model, but actual routing remains independently unverifiable.
- Baseline changed-file snapshot and hashes are under `build/acceptance/model-architecture-hardening-20261005/`.

## Release rule

Any P0 failure blocks delivery. Windows real-device, clean package, real capture closed-sidecar, and real Guandan checkpoint/runtime checks remain `unverified` unless directly evidenced. This record must not be bulk-marked passed before execution.
## Final candidate validation — 2026-10-05

- Python 3.12 x64: exit 0.
- `py -3.12 -m pytest -p no:cacheprovider -q`: exit 0, 447 passed, 1 skipped, 5 deselected.
- `py -3.12 -m pytest -p no:cacheprovider tests/acceptance tests/ui_acceptance -q`: exit 0, 29 passed.
- `py -3.12 -m pytest -p no:cacheprovider tests/presentation tests/integration/test_acceptance_runtime.py::test_floating_layout_is_persisted_and_restored -q`: exit 0, 76 passed, 1 skipped.
- `py -3.12 -B -m pytest -m local_capture -q -p no:cacheprovider`: exit 0, 5 passed.
- `py -3.12 -B scripts/run_local_capture_acceptance.py`: exit 2, status `unverified`. All five hashes and decision counts are stable; canonical coverage and capture-closed sidecar evidence remain incomplete.
- `py -3.12 -B -m compileall -q src`: exit 0.
- `py -3.12 -B -m py_compile Guance.spec scripts/build_release.py scripts/run_local_capture_acceptance.py`: exit 0.
- `git diff --check`: exit 0.
- `git ls-files -- debug/captures`: no tracked capture files.

## Submission gate

The user requested commit/push only after all tests and acceptance pass. That gate is **not satisfied** because ARCH-01/09/15/16/18 remain unverified, most importantly the explicit local-capture script returns `unverified` and clean Windows packaging/real-device checks were not executed. No commit or push was performed.

No real Guandan checkpoint exists; Guandan production remains explicitly unavailable. No automatic play/clicking, network replay/traffic modification, credentials/hidden-hand export, capture copy/move, or weight generation was performed.

## Follow-up acceptance items

- GUI-H01: passed by the full presentation/UI acceptance suite; Windows native DPI/visual behavior remains unverified.
- CAP-H01: pending/unverified until the five exact captures have canonical per-decision coverage and independent closed sidecar evidence.
- PKG-H01: pending/unverified until clean PyInstaller archive validation and Windows startup checks run.
- CORE-H01: structural candidate validation passed; complete provider-specific wildcard/lead-beating semantics remain a future Guandan checkpoint integration requirement.
