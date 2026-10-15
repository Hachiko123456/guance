from __future__ import annotations

from dataclasses import replace

import pytest

from guance.infrastructure.model.marvel_v2 import (
    CacheLifecycleError, ExactHistoryJournal, ExchangeTransferFacts,
    HistoryStep, HistoryWorkspace, MarvelEvent, UnsupportedStateError, encode_event,
)

ARTIFACT_SHA = "a" * 64


def _play_events() -> tuple[MarvelEvent, ...]:
    return (
        MarvelEvent("RESET", "LEAD_PLAY", 2, level=1),
        MarvelEvent("PRIVATE_PREFIX", "LEAD_PLAY", 2, tokens=(8,)),
        MarvelEvent("PRIVATE_PREFIX", "LEAD_PLAY", 2, tokens=(18,)),
        MarvelEvent("PRIVATE_PREFIX", "LEAD_PLAY", 2, tokens=(39,)),
        MarvelEvent("PRIVATE_PREFIX", "LEAD_PLAY", 2, tokens=(64,)),
        MarvelEvent("PUBLIC_COMMIT", "LEAD_PLAY", 2, tokens=(8, 18, 39, 64, 7)),
    )


def _journal(*, events=None, max_steps=4096, state_id="state-1") -> ExactHistoryJournal:
    return ExactHistoryJournal.from_events(
        session_id="session-1", game_id="game-1", state_id=state_id,
        artifact_id="iter000100", artifact_sha256=ARTIFACT_SHA,
        events=_play_events() if events is None else events, max_steps=max_steps,
    )


def test_exact_history_preserves_public_and_private_visibility():
    journal = _journal()
    assert len(journal.steps) == 6
    assert len(set(journal.steps[0].rows)) == 1
    assert len(set(journal.steps[-1].rows)) == 1
    for step in journal.steps[1:-1]:
        assert step.rows[2] != (0,) * 48
        assert tuple(index for index, row in enumerate(step.rows) if any(row)) == (2,)
    assert journal.cursor.token_step == 5
    assert journal.cursor.committed_step == 1
    assert journal.cursor.prefix_stage == 0


def _exchange_events() -> tuple[MarvelEvent, ...]:
    return (
        MarvelEvent("RESET", "TRIBUTE", 1, level=2),
        MarvelEvent("PRIVATE_PREFIX", "TRIBUTE", 1, tokens=(69,)),
        MarvelEvent("PRIVATE_EXCHANGE_COMMIT", "TRIBUTE", 1, tokens=(69, 7)),
        MarvelEvent("PRIVATE_PREFIX", "RETURN", 0, tokens=(70,)),
        MarvelEvent(
            "EXCHANGE_RESOLVED", "RETURN", 0,
            transfers=(
                ExchangeTransferFacts("tribute", 1, 0, 5),
                ExchangeTransferFacts("return", 0, 1, 6),
            ),
            last_actor=0,
        ),
    )


def test_exchange_prefix_private_commit_and_public_resolution_context():
    journal = _journal(events=_exchange_events())
    assert tuple(index for index, row in enumerate(journal.steps[1].rows) if any(row)) == (1,)
    assert tuple(index for index, row in enumerate(journal.steps[2].rows) if any(row)) == (1,)
    assert tuple(index for index, row in enumerate(journal.steps[3].rows) if any(row)) == (0,)
    assert len(set(journal.steps[4].rows)) == 1
    assert journal.cursor.token_step == 4
    assert journal.cursor.committed_step == 2
    assert journal.cursor.prefix_stage == 0
    assert journal.cursor.pending_exchange_count == 0


def test_exchange_commit_requires_exact_open_prefix_and_resolution_adds_final_commit():
    reset = MarvelEvent("RESET", "TRIBUTE", 1, level=2)
    prefix = MarvelEvent("PRIVATE_PREFIX", "TRIBUTE", 1, tokens=(69,))
    with pytest.raises(CacheLifecycleError, match="history_exchange_commit_prefix_mismatch"):
        _journal(events=(reset, prefix, MarvelEvent(
            "PRIVATE_EXCHANGE_COMMIT", "TRIBUTE", 1, tokens=(70, 7)
        )))
    with pytest.raises(CacheLifecycleError, match="history_exchange_resolution_without_prefix"):
        _journal(events=(reset, MarvelEvent(
            "EXCHANGE_RESOLVED", "TRIBUTE", 1,
            transfers=(ExchangeTransferFacts("tribute", 1, 0, 5),), last_actor=1,
        )))


def test_exchange_resolution_records_must_match_commit_order_and_final_prefix():
    events = list(_exchange_events())
    events[-1] = MarvelEvent(
        "EXCHANGE_RESOLVED", "RETURN", 0,
        transfers=(
            ExchangeTransferFacts("return", 0, 1, 6),
            ExchangeTransferFacts("tribute", 1, 0, 5),
        ),
        last_actor=0,
    )
    with pytest.raises(CacheLifecycleError, match="history_exchange_resolution_mismatch"):
        _journal(events=tuple(events))
    events = list(_exchange_events())
    events[-1] = MarvelEvent(
        "EXCHANGE_RESOLVED", "RETURN", 0,
        transfers=(
            ExchangeTransferFacts("tribute", 1, 0, 5),
            ExchangeTransferFacts("return", 0, 1, 7),
        ),
        last_actor=0,
    )
    with pytest.raises(CacheLifecycleError, match="history_exchange_resolution_mismatch"):
        _journal(events=tuple(events))


def test_observer_stream_rebuild_is_exact_and_deterministic():
    continuous = _journal(state_id="state-final")
    rebuilt = ExactHistoryJournal.from_observer_streams(
        session_id=continuous.session_id, game_id=continuous.game_id,
        state_id=continuous.state_id, artifact_id=continuous.artifact_id,
        artifact_sha256=continuous.artifact_sha256,
        observer_streams=continuous.observer_streams,
    )
    assert rebuilt.steps == continuous.steps
    assert rebuilt.history_sha256 == continuous.history_sha256
    assert rebuilt.cursor == continuous.cursor
    assert rebuilt.identity.cache_key == continuous.identity.cache_key


@pytest.mark.parametrize("streams", [None, (), ((), (), ()), ((), (), (), ())])
def test_missing_exact_observer_history_is_unavailable(streams):
    with pytest.raises(UnsupportedStateError):
        ExactHistoryJournal.from_observer_streams(
            session_id="session-1", game_id="game-1", state_id="state-1",
            artifact_id="iter000100", artifact_sha256=ARTIFACT_SHA,
            observer_streams=streams,
        )


def test_unequal_or_tampered_observer_visibility_is_rejected():
    journal = _journal()
    streams = [list(stream) for stream in journal.observer_streams]
    streams[0].pop()
    with pytest.raises(UnsupportedStateError):
        ExactHistoryJournal.from_observer_streams(
            session_id="session-1", game_id="game-1", state_id="state-1",
            artifact_id="iter000100", artifact_sha256=ARTIFACT_SHA,
            observer_streams=streams,
        )
    streams = [list(stream) for stream in journal.observer_streams]
    streams[3][-1] = (0,) * 48
    with pytest.raises(CacheLifecycleError, match="public_event_visibility_invalid"):
        ExactHistoryJournal.from_observer_streams(
            session_id="session-1", game_id="game-1", state_id="state-1",
            artifact_id="iter000100", artifact_sha256=ARTIFACT_SHA,
            observer_streams=streams,
        )
    streams = [list(stream) for stream in journal.observer_streams]
    streams[0][1], streams[2][1] = streams[2][1], (0,) * 48
    with pytest.raises(CacheLifecycleError, match="private_event_actor_visibility_invalid"):
        ExactHistoryJournal.from_observer_streams(
            session_id="session-1", game_id="game-1", state_id="state-1",
            artifact_id="iter000100", artifact_sha256=ARTIFACT_SHA,
            observer_streams=streams,
        )


def test_nonpass_public_commit_without_private_prefix_is_rejected():
    with pytest.raises(CacheLifecycleError, match="history_commit_prefix_mismatch"):
        _journal(events=(
            MarvelEvent("RESET", "LEAD_PLAY", 2, level=1),
            MarvelEvent("PUBLIC_COMMIT", "LEAD_PLAY", 2, tokens=(8, 7)),
        ))


def test_commit_must_match_exact_private_prefix_not_only_length():
    events = list(_play_events())
    events[-1] = MarvelEvent("PUBLIC_COMMIT", "LEAD_PLAY", 2,
                             tokens=(8, 19, 39, 64, 7))
    with pytest.raises(CacheLifecycleError, match="history_commit_prefix_mismatch"):
        _journal(events=tuple(events))


def test_identity_is_complete_bounded_and_changes_with_state_or_branch():
    journal = _journal()
    identity = journal.identity
    identity.validate()
    assert identity.session_id == "session-1"
    assert identity.game_id == "game-1"
    assert identity.state_id == "state-1"
    assert identity.artifact_id == "iter000100"
    assert identity.history_sha256 == journal.history_sha256
    assert identity.event_count == 6
    assert len(identity.cache_key) == 64
    assert replace(journal, state_id="state-2").identity.cache_key != identity.cache_key
    assert journal.clone_branch("branch-a").identity.cache_key != identity.cache_key
    with pytest.raises(CacheLifecycleError, match="history_session_id_invalid"):
        replace(journal, session_id="x" * 129)


def test_speculative_branch_clone_is_independent_and_confirmed_is_unchanged():
    confirmed = _journal(events=(_play_events()[0],))
    workspace = HistoryWorkspace(confirmed).clone_branch("branch-a")
    workspace = workspace.append_speculative(
        "branch-a", MarvelEvent("PRIVATE_PREFIX", "LEAD_PLAY", 2, tokens=(8,)),
        state_id="state-spec-a",
    )
    workspace = workspace.clone_branch("branch-b", source_branch_id="branch-a")
    workspace = workspace.append_speculative(
        "branch-b", MarvelEvent("PRIVATE_PREFIX", "LEAD_PLAY", 2, tokens=(18,)),
        state_id="state-spec-b",
    )
    assert workspace.confirmed == confirmed
    assert workspace.confirmed.cursor.token_step == 0
    assert workspace.branch("branch-a").identity.token_step == 1
    assert workspace.branch("branch-b").identity.token_step == 2
    assert workspace.branch("branch-a").extra_steps != workspace.branch("branch-b").extra_steps
    assert workspace.branch("branch-a").state_id == "state-spec-a"
    assert workspace.branch("branch-b").state_id == "state-spec-b"


def test_confirmed_observation_expires_speculative_branches():
    confirmed = _journal(events=(_play_events()[0],))
    workspace = HistoryWorkspace(confirmed).clone_branch("branch-a")
    updated = workspace.append_confirmed(
        MarvelEvent("PRIVATE_PREFIX", "LEAD_PLAY", 2, tokens=(8,)),
        state_id="state-observed",
    )
    assert updated.branches == ()
    assert updated.confirmed.state_id == "state-observed"
    assert updated.confirmed.cursor.token_step == 1
    assert workspace.confirmed.cursor.token_step == 0


def test_history_horizon_branch_limit_and_prefix_overflow_fail_explicitly():
    journal = _journal(events=(_play_events()[0],), max_steps=2)
    journal = journal.append(_play_events()[1])
    with pytest.raises(UnsupportedStateError, match="history_horizon_exceeded"):
        journal.append(_play_events()[2])
    workspace = HistoryWorkspace(
        _journal(events=(_play_events()[0],)), max_branches=1
    ).clone_branch("branch-a")
    with pytest.raises(UnsupportedStateError, match="branch_limit_exceeded"):
        workspace.clone_branch("branch-b")
    events = [MarvelEvent("RESET", "LEAD_PLAY", 2, level=1)]
    events.extend(MarvelEvent("PRIVATE_PREFIX", "LEAD_PLAY", 2, tokens=(8,))
                  for _ in range(33))
    with pytest.raises(UnsupportedStateError, match="history_prefix_overflow"):
        _journal(events=tuple(events), max_steps=40)


def test_empty_journal_does_not_claim_exact_history_or_identity():
    journal = ExactHistoryJournal(
        "session-1", "game-1", "state-1", "iter000100", ARTIFACT_SHA
    )
    assert journal.exact is False
    with pytest.raises(UnsupportedStateError, match="exact_observer_history_required"):
        _ = journal.identity



def test_reset_binds_first_action_actor_and_phase_but_commit_releases_context():
    with pytest.raises(CacheLifecycleError, match="history_action_context_mismatch"):
        _journal(events=(
            MarvelEvent("RESET", "LEAD_PLAY", 0, level=1),
            MarvelEvent("PRIVATE_PREFIX", "LEAD_PLAY", 2, tokens=(8,)),
        ))
    with pytest.raises(CacheLifecycleError, match="history_action_context_mismatch"):
        _journal(events=(
            MarvelEvent("RESET", "FOLLOW_PLAY", 0, level=1),
            MarvelEvent("PUBLIC_COMMIT", "FOLLOW_PLAY", 2, tokens=(6,)),
        ))
    journal = _journal(events=(
        MarvelEvent("RESET", "FOLLOW_PLAY", 0, level=1),
        MarvelEvent("PUBLIC_COMMIT", "FOLLOW_PLAY", 0, tokens=(6,)),
        MarvelEvent("PRIVATE_PREFIX", "LEAD_PLAY", 2, tokens=(8,)),
    ))
    assert journal.cursor.actor == 2
    assert journal.cursor.phase == "LEAD_PLAY"
    assert journal.cursor.context_bound is True


def test_history_objects_reject_mutable_nested_containers_and_copy_stream_inputs():
    reset_rows = encode_event(MarvelEvent("RESET", "LEAD_PLAY", 0, level=1))
    with pytest.raises(CacheLifecycleError, match="observer_history_row_type_invalid"):
        HistoryStep(tuple(list(row) for row in reset_rows))
    confirmed = _journal(events=(MarvelEvent("RESET", "LEAD_PLAY", 0, level=1),))
    with pytest.raises(CacheLifecycleError, match="branch_collection_type_invalid"):
        HistoryWorkspace(confirmed, branches=[])
    branch = confirmed.clone_branch("branch-a")
    with pytest.raises(CacheLifecycleError, match="branch_steps_type_invalid"):
        replace(branch, extra_steps=[])

    streams = [[list(row) for row in stream] for stream in confirmed.observer_streams]
    rebuilt = ExactHistoryJournal.from_observer_streams(
        session_id=confirmed.session_id, game_id=confirmed.game_id,
        state_id=confirmed.state_id, artifact_id=confirmed.artifact_id,
        artifact_sha256=confirmed.artifact_sha256, observer_streams=streams,
    )
    before = rebuilt.history_sha256
    streams[0][0][0] = 99
    assert rebuilt.history_sha256 == before
    assert rebuilt.steps[0].rows[0][0] == 1



def test_history_step_rejects_noncanonical_reset_level_rank_and_private_phase_token():
    rows = [list(row) for row in encode_event(MarvelEvent("RESET", "LEAD_PLAY", 0, level=1))]
    for row in rows:
        row[3] = 33  # SUIT token is never a valid reset level-rank token
    with pytest.raises(CacheLifecycleError, match="history_reset_level_invalid"):
        HistoryStep(tuple(tuple(row) for row in rows))

    rows = [list(row) for row in encode_event(
        MarvelEvent("PRIVATE_PREFIX", "LEAD_PLAY", 0, tokens=(8,))
    )]
    rows[0][2] = 4  # mutate the private row to TRIBUTE while retaining a family token
    with pytest.raises(CacheLifecycleError, match="history_private_prefix_invalid"):
        HistoryStep(tuple(tuple(row) for row in rows))


def test_history_cursor_context_is_hashed_into_receipt_content():
    committed = _journal(events=(
        MarvelEvent("RESET", "FOLLOW_PLAY", 0, level=1),
        MarvelEvent("PUBLIC_COMMIT", "FOLLOW_PLAY", 0, tokens=(6,)),
    ))
    rebound = committed.append(
        MarvelEvent("PRIVATE_PREFIX", "LEAD_PLAY", 2, tokens=(8,))
    )
    assert committed.cursor.context_bound is False
    assert rebound.cursor.context_bound is True
    assert committed.identity.cache_key != rebound.identity.cache_key



def test_phase_family_transitions_fail_closed_and_exchange_resolution_enters_lead():
    with pytest.raises(CacheLifecycleError, match="history_phase_transition_invalid"):
        _journal(events=(
            *_play_events(),
            MarvelEvent("PRIVATE_PREFIX", "TRIBUTE", 1, tokens=(69,)),
        ))
    with pytest.raises(CacheLifecycleError, match="history_phase_transition_invalid"):
        _journal(events=(
            MarvelEvent("RESET", "TRIBUTE", 1, level=1),
            MarvelEvent("PRIVATE_PREFIX", "TRIBUTE", 1, tokens=(69,)),
            MarvelEvent("PRIVATE_EXCHANGE_COMMIT", "TRIBUTE", 1, tokens=(69, 7)),
            MarvelEvent("PRIVATE_PREFIX", "LEAD_PLAY", 2, tokens=(8,)),
        ))
    with pytest.raises(CacheLifecycleError, match="history_phase_transition_invalid"):
        _journal(events=(
            MarvelEvent("RESET", "RETURN", 0, level=1),
            MarvelEvent("PRIVATE_PREFIX", "RETURN", 0, tokens=(70,)),
            MarvelEvent("PRIVATE_EXCHANGE_COMMIT", "RETURN", 0, tokens=(70, 7)),
            MarvelEvent("PRIVATE_PREFIX", "TRIBUTE", 1, tokens=(69,)),
        ))
    resolved = _journal(events=_exchange_events())
    with pytest.raises(CacheLifecycleError, match="history_phase_transition_invalid"):
        resolved.append(MarvelEvent("PRIVATE_PREFIX", "FOLLOW_PLAY", 2, tokens=(8,)))
    lead = resolved.append(MarvelEvent("PRIVATE_PREFIX", "LEAD_PLAY", 2, tokens=(8,)))
    assert lead.cursor.phase == "LEAD_PLAY"
    assert lead.cursor.actor == 2
