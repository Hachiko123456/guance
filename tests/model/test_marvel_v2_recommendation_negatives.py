"""Phase 3 recommendation negatives: illegal selection, no-commit, oracle beat.

Model outputs are faked ONLY here to exercise the bounded failure paths; the
positive gate always uses the real iter2000 checkpoint (see the acceptance
module).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from guance.infrastructure.model.errors import ModelBackendError  # noqa: E402
from guance.infrastructure.model.marvel_v2.decision_request import (  # noqa: E402
    parse_confirmed_decision,
)
from guance.infrastructure.model.marvel_v2.recommendation import (  # noqa: E402
    run_confirmed_recommendation,
)
from guance.infrastructure.model.marvel_v2.rules_oracle import (  # noqa: E402
    RulesOracleError, VerifiedGuandanRulesOracle,
)

import tests.integration.test_phase3_confirmed_recommendation as acceptance  # noqa: E402


@pytest.fixture(scope="module")
def fixtures():
    return acceptance._synthetic_fixtures()


def _torch():
    return pytest.importorskip("torch")


class _FakeRuntime:
    def __init__(self, probabilities):
        self._torch = _torch()
        self._probabilities = probabilities
        self.cached_calls = 0

    @property
    def device(self):
        return self._torch.device("cpu")

    def forward_confirmed_stepwise(self, fields, *, timeout_seconds=None):
        return self._probabilities, object()

    def clone_confirmed_cache(self, cache):
        return object()

    def forward_confirmed_cached_step(self, cache, fields, *, timeout_seconds=None):
        self.cached_calls += 1
        return self._probabilities


class _SequenceRuntime:
    """Fake runtime returning a scripted probability vector per autoregressive step."""

    def __init__(self, first, rest):
        self._torch = _torch()
        self._first = first
        self._rest = list(rest)
        self.cached_calls = 0

    @property
    def device(self):
        return self._torch.device("cpu")

    def forward_confirmed_stepwise(self, fields, *, timeout_seconds=None):
        return self._first, object()

    def clone_confirmed_cache(self, cache):
        return object()

    def forward_confirmed_cached_step(self, cache, fields, *, timeout_seconds=None):
        value = self._rest.pop(0)
        self.cached_calls += 1
        return value


def test_path_score_is_cumulative_log_likelihood_not_the_last_token(fixtures):
    import math

    payload, _context = fixtures[0]  # lead-single-only: [FAMILY, RANK, CARD, COMMIT]
    parsed = parse_confirmed_decision(payload)
    torch = _torch()
    runtime = _SequenceRuntime(
        torch.tensor([0.2]),
        [torch.tensor([0.8]), torch.tensor([0.5]), torch.tensor([0.5])])
    outcome = run_confirmed_recommendation(runtime, parsed,
                                           rules_root=acceptance.RUNTIME / "source")
    expected = math.log(0.2) + math.log(0.8) + math.log(0.5) + math.log(0.5)
    assert outcome.selected_token_count == 4
    assert abs(outcome.path_score - expected) < 1e-6  # float32 model probabilities
    assert outcome.path_score != pytest.approx(0.5)
    assert outcome.path_score <= 0.0


def test_illegal_or_out_of_range_row_selection_is_bounded(fixtures):
    payload, _context = fixtures[0]
    parsed = parse_confirmed_decision(payload)
    torch = _torch()
    rows = torch.zeros(parsed.legal_row_count + 1)
    rows[-1] = 1.0  # argmax points outside the legal rows
    with pytest.raises(ModelBackendError) as exc:
        run_confirmed_recommendation(_FakeRuntime(rows), parsed,
                                     rules_root=acceptance.RUNTIME / "source")
    assert exc.value.code == "MODEL_OUTPUT_INVALID"


def test_non_committing_selection_cannot_produce_a_partial_action(fixtures):
    payload, _context = fixtures[1]  # lead-pair-only: first token is non-commit
    parsed = parse_confirmed_decision(payload)
    torch = _torch()
    rows = torch.zeros(parsed.legal_row_count)
    rows[0] = 1.0

    class _FailingCacheRuntime(_FakeRuntime):
        def forward_confirmed_cached_step(self, cache, fields, *, timeout_seconds=None):
            raise ModelBackendError("MODEL_TIMEOUT")

    with pytest.raises(ModelBackendError) as exc:
        run_confirmed_recommendation(_FailingCacheRuntime(rows), parsed,
                                     rules_root=acceptance.RUNTIME / "source")
    assert exc.value.code == "MODEL_TIMEOUT"


def test_recommendation_entry_revalidates_topology_before_model_work(fixtures):
    """Defense in depth: an inconsistent ParsedConfirmedDecision never reaches the model.

    The parser cannot produce this object; a compromised caller is simulated with
    dataclasses.replace so the recommendation entry must fail closed on its own.
    """
    from dataclasses import replace

    payload, _context = fixtures[9]  # follow-single (FOLLOW_PLAY)
    parsed = parse_confirmed_decision(payload)
    impossible = replace(parsed, marvel_state=replace(
        parsed.marvel_state,
        current_winner_seat=parsed.marvel_state.active_seat))

    class _NoModelWorkRuntime(_FakeRuntime):
        def forward_confirmed_stepwise(self, fields, *, timeout_seconds=None):
            raise AssertionError("model work must not start on impossible topology")

    torch = _torch()
    runtime = _NoModelWorkRuntime(torch.zeros(1))
    with pytest.raises(ModelBackendError) as exc:
        run_confirmed_recommendation(runtime, impossible,
                                     rules_root=acceptance.RUNTIME / "source")
    assert exc.value.code == "MODEL_INPUT_UNSUPPORTED"


def test_rules_oracle_rejects_non_beating_play():
    oracle = VerifiedGuandanRulesOracle(acceptance.RUNTIME / "source")
    from guance.public_state_contract import guandan_card_id_to_wire, wire_card_to_guandan_id
    from guance.infrastructure.model.marvel_v2.rules_oracle import _public_declaration

    def wc(suit, rank):
        return wire_card_to_guandan_id((suit << 4) | rank)

    level = 2
    winner = oracle.legal_action_candidates((wc(1, 9),), wire_level=level,
                                            phase="LEAD_PLAY", current=None)[0]
    smaller = oracle.legal_action_candidates((wc(1, 8),), wire_level=level,
                                             phase="LEAD_PLAY", current=None)[0]
    current = _public_declaration(winner.declaration)
    with pytest.raises(RulesOracleError) as exc:
        oracle.validate_resolved_play(smaller.declaration, wire_level=level, current=current)
    assert exc.value.code == "LIVE_PLAY_DOES_NOT_BEAT"
    oracle.validate_resolved_play(winner.declaration, wire_level=level, current=None)
