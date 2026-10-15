# MODEL-BACKEND-ARCHITECTURE-20261005 — task acceptance mapping

## Scope and baseline

- Original request: decouple Guance model integration behind `ModelService`, generic JSON Lines worker, manifest model bundles, Provider Registry + Abstract Factory, Strategy and Adapter/ACL boundaries; preserve FableDan `.npz` compatibility; add a test-only/unavailable Guandan provider scaffold; add privacy-safe local-capture selectors and documentation.
- Date/timezone: 2026-10-05, Asia/Shanghai.
- Repository: `D:\project\guance`.
- Related read-only training project: `D:\project\guandan`.
- Pre-existing worktree changes: training-data docs/source/tests and edits to `README.md`, `docs/ARCHITECTURE.md`, and `src/guance/application/controller.py`; these are not to be reverted or absorbed as model-backend work.
- Main-agent ownership: this record and final acceptance decision.
- Candidate implementation must not modify, copy, move, or track `debug/captures`; no model weights, payloads, full converted games, hidden hands, cookies, tokens, authorization headers, or private keys may be added.

## Acceptance items

| ID | Source requirement / priority | Prerequisites and input | Observable expected result | Verification and required evidence | Status |
|---|---|---|---|---|---|
| MBA-P0-001 | AGENTS P0-009; no silent fallback | Missing/corrupt/incompatible bundle or provider failure | Explicit unavailable/error; no rule/fake/provider fallback in production path | Focused ModelService, manifest, worker and no-fallback tests; inspect diff | passed |
| MBA-P0-002 | AGENTS P0-006; privacy boundary | Canonical observation and serialized worker payload | Only public information plus own hand; no hidden opponent hand, post-state, settlement, reward, future action, payload or auth data | Contract serialization/privacy tests; static search; inspect worker messages | passed |
| MBA-P0-003 | User: preserve FableDan | Existing valid `.npz` and legacy advisor tests | FableDan remains default-compatible; legacy loading and AdviceResult fields remain compatible; controller does not import concrete advisor | FableDan compatibility tests, existing model/integration tests, import graph inspection | passed |
| MBA-P0-004 | User: stable generic protocol | Worker load/recommend/stop messages | Generic worker emits loaded/status/result/error; request_id and state_version preserved; malformed or oversized input fails closed | JSONL worker/client tests; focused pytest output | passed |
| MBA-P0-005 | User: ModelService facade | Controller lifecycle/import/status operations | Controller depends on ModelService port/facade, not weights path or provider advisor; lifecycle and stale invalidation preserved | ModelService lifecycle tests and controller import inspection | passed |
| MBA-P0-006 | User: strict manifest bundle | `manifest.json`, artifact and optional `SHA256SUMS` | Missing fields, hash mismatch, incompatible protocol, unknown backend/provider/runtime reject load; no ready without artifact | Manifest unit tests and filesystem fixture tests; no tracked weights | passed |
| MBA-P1-001 | User: provider registry/factory | FableDan, Guandan and fake/test provider registrations | Registry resolves provider from manifest; factory creates adapter/runtime; adding provider does not change controller flow | Registry/factory tests; inspect registration boundary | passed |
| MBA-P1-002 | User: CanonicalPublicGameView | Valid and ambiguous `GameObservation` | Canonical public view includes required fields, explicit `public_plus_self` visibility, completeness/ambiguities; ambiguous/non-unique public action fails closed | Contract/privacy/adapter tests | passed |
| MBA-P1-003 | User: CanonicalAdviceResult | FableDan and fake/Guandan candidates | AdviceCandidate supports `score_kind` and metadata; raw score scales remain provider-labelled; claim remains compatible and optional | Contract compatibility and provider tests | passed |
| MBA-P1-004 | User: Guandan provider scaffold | No real Guandan checkpoint | GuandanProvider/Advisor/runtime protocol/observation adapter/token decoder exist; PASS/COMMIT, legal mask, top-N and search strategy seams are tested; unavailable without real artifact | Guandan fake-runtime and adapter tests; explicit unavailable status | passed (real runtime unverified) |
| MBA-P1-005 | User: latest-only/stale | Successive observations and delayed worker replies | Latest-only queue, request IDs, state-version checks and stale-result rejection remain enforced | Existing and new client tests; focused pytest evidence | passed |
| MBA-P1-006 | User: configuration/UI/packaging decoupling | Default startup and model import paths | Generic bundle/manifest metadata surfaced; legacy `.npz` accepted; no fixed `best.npz` packaging dependency; optional Guandan runtime lazy/unavailable | Config/controller/UI/spec static review, import smoke, relevant tests | passed (package build unverified) |
| MBA-P1-007 | User: local capture regression safety | Five fixed local capture paths | Selector metadata only; source hash/immutability/fail-closed checks; missing capture is explicit failed/unverified, never silent pass; no capture tracked | Selector unit/static tests, explicit `pytest -m local_capture`, `git ls-files debug/captures` | passed (closed sidecars 3 unverified) |
| MBA-P2-001 | User: documentation | Final architecture and acceptance docs | `docs/MODEL_BACKENDS.md` and this record describe interfaces, responsibilities, no-fallback, fake runtime, local capture, and future checkpoint steps accurately | Document review against actual code and tests | passed |
| MBA-P2-002 | AGENTS P2-004/P2-006 | PyInstaller config and repository state | FableDan remains packagable without torch; lazy optional provider imports; no weights/capture/payload in Git | `git diff --check`, `git status --short`, `git ls-files debug/captures`, packaging/static review | passed (full package unverified) |

## Required command evidence

The main agent must record exact commands and exit codes for:

- focused model/provider/manifest/worker/privacy tests;
- relevant integration and FableDan compatibility tests;
- explicit local-capture marker run;
- full `py -3.12 -m pytest -q`;
- `py -3.12 -m compileall -q src`;
- `git diff --check`;
- `git ls-files debug/captures`;
- final `git status --short` and untracked-file inspection.

## Status vocabulary and release rule

- `passed`: command/evidence directly verifies the criterion in the candidate tree.
- `failed`: criterion is contradicted by an executed check or inspected code.
- `skipped`: deliberately not run because an explicitly documented environment prerequisite is absent.
- `unverified`: no safe evidence yet, including missing real checkpoints/captures or Windows-only runtime behavior.
- No final “complete” claim is allowed while any blocking MBA-P0 item is failed or unverified.

## Frozen interface and input acceptance details (main-owned)

- Manifest schema 1 requires all 18 user-listed fields (schema_version, backend, provider, artifact, artifact_format, input_contract, output_contract, model_version, training_version, rules_version, action_version, environment_version, encoding_version, algorithm, architecture, runtime, artifact_sha256, capabilities). Optional protocol_version defaults to 1 for schema 1; explicitly incompatible values reject. Capabilities supports the requested strict boolean mapping. Artifact paths must remain within the bundle, including resolved symlinks/Windows paths; hash mismatch and unsupported provider/version/runtime reject without fallback. SHA256SUMS, when present, must agree.
- Canonical wire is public-play-only, not a full environment: explicit public_plus_self, validated completeness, no ambiguity list entries accepted for inference, preserved free_lead, monotonic public actions and version identity. Guandan A04 cannot be synthesized from missing previous-result/declaration/exchange fields.
- Generic protocol uses bounded JSONL with protocol version and request ID; load/recommend/stop must never admit an earlier generation's response after invalidate/reimport/close. The client must check response identity, not just submitted observation identity. Metadata and errors use a bounded allowlist.
- No fake weights may be created; test transports/runtimes are explicit synthetic objects, never default production registry entries. Guandan production remains unavailable even if an arbitrary artifact file is present, until a real runtime and public-information ACL exist.
- Capture candidate selectors (all paths are `debug/captures/<capture_id>/frames.jsonl`):

| capture_id | source_sha256 | level | observer | decisions | expected quality |
|---|---|---:|---:|---:|---|
| 20260929-162605 | b497b4b426a2a070ad55d7c519b18d2832c335d20784c97bfe7a6d6bb9c9b92b | 7 | 3 | 20 | accepted_candidate; settled; closed unverified |
| 20260929-173126 | 7c001006fd115e0a3e3bc89328e3e8b2073348afd88a8ac8be7223ae1e72aff7 | 6 | 0 | 21 | accepted_candidate; settled; closed unverified |
| 20260929-232820 | 729f1b7f4724e150cbc631dbed380514fbcb088222a0cb3ab7bc9d830348d78f | 4 | 2 | 17 | accepted_candidate; settled; closed unverified |
| 20260930-012733 | cce2715b2eaf832014dfcd061df8482eba706d8f0e4c876bf75f1272155fe703 | 8 | 2 | 21 | accepted_candidate; settled; closed |
| 20260930-171722 | 706a0331e74ca5bcca251c80b5c0502d6a2f10d72211d06d0eeb0bd750498302 | 7 | 1 | 24 | accepted_candidate; settled; closed |

Capture acceptance additionally requires: all 5 exact sources, one episode each, no errors/lost/quarantine/partial, monotonic state/action indexes, valid pre-state, each decision adapters supported or explicit unsupported, fake contract outputs legal and identity-matched, coverage seats 0..3 / levels 4,6,7,8 / PASS / single / pair / consecutive / bomb or complex. No full game output or future/outcome fields sent into model. Missing sources must fail explicit local_capture runs, never skip as pass.

## Baseline evidence and workflow limitations

- Main baseline `py -3.12 -m pytest tests/model tests/training_data tests/integration -q -p no:cacheprovider`: exit 1, 79 passed / 1 failed. Pre-existing failure: floating_layout restore adds sizeMode=manual whereas test expects exact old dict. No tests weakened or production floating behavior changed to hide it.
- Main independently read all five sources via read_legacy_episodes and compared SHA256 before/after: exit 0; five episodes, 103 accepted candidates, 0 partial/quarantine/errors, all five hashes match the table. This is preflight evidence, not final provider acceptance.
- Native audit agents were requested explicitly as gpt-6-astra/ultra. Initial architecture/test launches hit HTTP 429; same-model continuation/replacement produced reports. Tool results do not attest the actual routed model/effort.
- Audit-process deviations: capture auditor ran existing synthetic pytest (31 passed) that wrote Temp fixtures despite read-only instructions; no real capture was copied/modified. Test auditor also ran collection and a four-test model smoke despite the static-only packet. No repository changes were attributed to these audits. These deviations do not qualify as a passed zero-write audit.
- Implementation was initially dispatched before all stage-0 reports, then immediately revoked before any changes; final core implementation began while the test-audit report was still pending. Acceptance mapping existed before any code. This is recorded as a sequencing deviation, not silently erased.
- Core implementer handoff assertions are not final evidence; MAIN reruns focused and full tests on the actual candidate.

## Final validation record

- Final model-focused tests: `py -3.12 -B -m pytest tests/model -q` — exit 0, 190 passed.
- Final explicit capture tests: `py -3.12 -B -m pytest -m local_capture -q` — exit 0, 5 passed, 419 deselected.
- Final read-only capture script: `py -3.12 -B scripts/run_local_capture_acceptance.py` — exit 0; five fixed sources, hashes unchanged, 103 accepted decisions.
- Final full pytest: exit 1, 416 passed, 1 skipped, 5 deselected, 2 failed. Both failures are the pre-existing floating-window `sizeMode: manual` mismatch in `tests/integration/test_acceptance_runtime.py::test_floating_layout_is_persisted_and_restored` and `tests/presentation/test_qml.py::test_floating_fit_grows_only_and_manual_close_flushes_geometry`; this task did not change or weaken those tests.
- Final compile/static checks: `py -3.12 -m compileall -q src`, `py_compile` for `Guance.spec`/build/local-capture scripts, `git diff --check`, and `git ls-files -- debug/captures` all exit 0.
- Production Guandan checkpoint/runtime: unavailable and intentionally not claimed.
- Windows frozen package and real GUI startup: unverified; no release acceptance claim.
