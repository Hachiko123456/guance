# Guance integration/runtime acceptance report

Date: 2026-09-28
Scope: controller/ViewModel signal boundary and fake-backed runtime acceptance.

## Scope and safety

- No production files changed.
- No real Windows proxy, certificate store, network, mitmdump, or WeChat process was touched.
- UI actions were exercised through ViewModel slots/signals and offscreen QML object state; no physical mouse automation was used.

## Matrix results

| Matrix | Result | Evidence |
|---|---:|---|
| UI-001..UI-014 | PASS | `tests/acceptance/test_ui_boundary_acceptance.py` (15 tests) |
| CORE-002 | PASS | stale advice is rejected at ViewModel boundary |
| CORE-003 | PASS | local `best.npz` loads and returns legal advice when present |
| CORE-004 | PASS | controller reports model worker error without a rule fallback |
| CORE-005 | PASS | direct capture config has no upstream proxy |
| CORE-006 | PASS | mixed upstream config is explicit and command contains no credentials |
| CORE-007 | PASS | certificate consent path is tested without side effects |
| CORE-008 | PASS | bounded/redacted diagnostics and export package checks |
| CORE-009 | PASS | proxy/runtime lifecycle ordering is checked with fakes |

## Focused result

```text
python -m pytest -q tests/acceptance tests/integration
25 passed
```

The broader non-GUI regression set also passed:

```text
python -m pytest -q tests/acceptance tests/integration tests/domain tests/model tests/protocol tests/runtime
37 passed
```

## Full-suite result

The Qt teardown issue was repaired by keeping QML engines/windows alive until the Qt test process exits instead of deleting nested Window objects during fixture teardown. The final presentation and full-suite runs are clean.

```text
python -m pytest -q tests/presentation tests/ui_acceptance tests/acceptance tests/integration
58 passed, 1 skipped

python -m pytest -q
70 passed, 1 skipped
process exit: 0
```

## Real-machine limitations

These tests do not prove the following UI acceptance items:

- Windows certificate consent in the real certificate store;
- Windows proxy takeover/restoration;
- real WeChat connection discovery;
- packaged EXE mouse interaction;
- real small-window display on the user's desktop.

Those remain manual Windows acceptance tasks.
