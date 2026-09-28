# Batch integration contracts

All workers read `src/guance/contracts.py`. The Lead owns it; request changes instead of editing.

## Core

`guance.domain.session.SessionEngine(session_id: str | None = None)`:
- `feed(frame: SourceFrame) -> list[EngineUpdate]`
- `reset(reason: str = "") -> None`
- `latest_observation: GameObservation | None`
- feed returns `state` updates on every confirmed mutation (including opponent turns), `observation` when advice should be requested, `ended`, and `error` on invalid state. `state_version` advances on state changes; never gate solely on our turn.

`guance.domain.cards.card_label(code: int) -> str`, `card_parts(code)->tuple[suit,rank,copy]`.

`guance.infrastructure.protocol.saved_capture.iter_game_frames(path) -> Iterator[SourceFrame]` is offline-only.

## GUI

`guance.presentation.viewmodel.GuanceViewModel(QObject)`:
- Signals: `startRequested()`, `stopRequested()`, `authorizeRequested()`, `repairRequested()`, `removeCertificateRequested()`, `modelImportRequested(str)`, `exportDiagnosticsRequested()`, `demoRequested()`, `floatingChanged(bool)`.
- Methods callable by Lead/controller: `apply_status(dict)`, `apply_observation(dict)`, `apply_advice(dict)`, `append_log(dict)`, `clear_advice(str = "")`.
- GUI can expose additional Qt properties, but no actual network/model/Windows imports.
- `guance.presentation.app.create_ui(app, viewmodel) -> engine` loads a visible QML ApplicationWindow. QML resources must work from source and a frozen bundle.
- No live capture or certificate actions during screenshots/tests. The demo is visibly marked as offline demonstration.

## Runtime

`guance.application.runtime.service.RuntimeService(paths, on_frame, on_status, ...)`:
- `inspect() -> dict`
- `prepare_certificate(consent: bool, owner_hwnd: int = 0) -> dict` (explicit consent; Windows confirmation may remain)
- `start() -> dict` (preflight trust and helper; atomic proxy switch only after listener ready)
- `stop() -> dict` (restore proxy first, then stop helper)
- `repair() -> dict`
- `remove_certificate(consent: bool, owner_hwnd: int = 0) -> dict`
- `close() -> None`
- `on_frame(SourceFrame)` and `on_status(dict)` may run off the UI thread.
- Constructor `paths` is a dictionary with `data_dir`, `runtime_dir`, `config_dir`, `logs_dir`, `cert_dir` (Path values). Capture helper located under `runtime_dir/mitmproxy/mitmdump.exe` or configured path.
- No real proxy edits, certificate changes, or TLS interception in this coding environment; tests use injected fakes. User executes the real desktop consent/test.

## Lead/model

`guance.infrastructure.model.advisor.FableDanAdvisor(weights_path, ...)`:
- `load() -> dict`
- `recommend(GameObservation, request_id='') -> AdviceResult`
- required model only, no rule fallback.

IPC records are bounded JSON; opaque payloads use base64. Logging is separate and redacted. Never commit model weights, raw .mitm files, tokens, CA material, or personal capture data.
