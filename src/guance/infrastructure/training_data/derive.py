"""Regenerable current-FableDan view. Never loads weights, Qt, or a training runtime."""
from __future__ import annotations

from collections import Counter
from typing import Any

from guance.contracts import GameObservation, PublicAction
from guance.infrastructure.model.advisor import build_model_observation, _resolve_action
from guance.infrastructure.model.fabledan_vendor.combos import PASS_MOVE
from guance.infrastructure.model.fabledan_vendor.encode import (
    FEAT_DIM, VOCAB, MAX_SEQ, tokenize, hand_action_features,
)

from .validation import validate_decision
from .schema import canonical_json, stable_id

ENCODER_REVISION = 'fabledan-7cc5e311b9860bc44f76c082d9c1b21fc8b2d3ec'
ADAPTER_VERSION = 1
REWARD_REVISION = 'fabledan-team-rank-normalized-v1'


def observation_from_record(record: dict[str, Any]) -> GameObservation:
    validate_decision(record)
    pre = record['pre_state']
    history = tuple(PublicAction(**a) for a in pre['history'])
    lead = None
    if not pre['free_lead']:
        candidates = [a for a in history if a.trick == pre['trick'] and a.kind == 'play']
        if not candidates:
            raise ValueError('missing_public_lead')
        lead = candidates[-1]
    return GameObservation(session_id=pre['session_id'],game_id=pre['game_id'],
        state_version=pre['state_version'],captured_at=pre['captured_at'],level=pre['level'],
        my_seat=pre['my_seat'],first_seat=pre['first_seat'],current_seat=pre['current_seat'],
        hand=tuple(pre['hand']),remaining=tuple(pre['remaining']),history=history,
        lead_action=lead,free_lead=pre['free_lead'],trick=pre['trick'])


def terminal_return(record: dict[str, Any]) -> float | None:
    """Same rank-to-team Monte Carlo return convention as the pinned engine, /3.

    This is an explicit derived target, not a per-move correctness score.
    Canonical source reward stays null. Missing/disputed outcomes stay missing.
    """
    if record['status'] != 'accepted_candidate' or record['outcome'] is None:
        return None
    ranks = record['outcome']['finish_ranks_by_seat']
    first = ranks.index(1)
    winners = {first, (first + 2) % 4}
    reward = 5 - ranks[(first + 2) % 4]
    return (reward if record['pre_state']['my_seat'] in winners else -reward) / 3.0


def derive_fabledan(record: dict[str, Any]) -> dict[str, Any]:
    """Compile one independently versioned training view; no hidden-hand targets.

    Generation is explicit/offline, not part of the latency-sensitive recorder.
    Semantic action resolution can fail even for a state-engine accepted record;
    callers must report unsupported samples instead of filling fake zero features.
    """
    validate_decision(record)
    if record['status'] == 'quarantine':
        raise ValueError('quarantined_record')
    if (record['provenance']['rule_profile'] != 'tencent_daguandan'
            or record['provenance']['rule_revision'] != 'guance-observed-v1'):
        raise ValueError('unsupported_rule_profile')
    observation = observation_from_record(record)
    obs = build_model_observation(observation)
    a = record['observed_action']
    action = PublicAction(**{k:a[k] for k in ('index','player','kind','cards','combo_type','next_player','trick')})
    if action.kind == 'pass':
        if obs['lead'] is None:
            raise ValueError('pass_on_free_lead')
        move = PASS_MOVE
    else:
        move = _resolve_action(action, observation.level - 1, obs['lead'])
        if Counter(move.cards) - Counter(obs['hand']):
            raise ValueError('action_not_in_hand')
    tokens = tokenize(obs['events'],obs['player'],obs['level'])
    features = hand_action_features(obs,move).tolist()
    exact = [i for i,m in enumerate(obs['legal']) if m.type==move.type and m.key==move.key
             and sorted(m.cards)==sorted(move.cards) and sorted(m.claim_ranks)==sorted(move.claim_ranks)]
    reward = terminal_return(record)
    result = {
        'record_type':'fabledan_derived_decision','adapter_version':ADAPTER_VERSION,
        'canonical_record_id':record['record_id'],'canonical_sha256':stable_id(record),
        'episode_group':record['provenance']['episode_id'],
        'encoder':{'revision':ENCODER_REVISION,'vocab_size':VOCAB,'feature_dim':FEAT_DIM,'max_tokens':MAX_SEQ},
        'inputs':{'tokens':tokens,'chosen_action_features':features},
        'action':{'signature':a['signature'],'model_combo_type':move.type,
                  'claim_ranks_zero_based':list(move.claim_ranks),
                  'generated_legal_count':len(obs['legal']),
                  'exact_generated_index':exact[0] if len(exact)==1 else None},
        'targets':{'terminal_return':reward,'reward_revision':REWARD_REVISION,
                   'masks':{'terminal_return':reward is not None,'belief_oracle':False}},
        'warnings':[] if len(exact)==1 else ['observed_physical_action_not_unique_in_generator'],
    }
    canonical_json(result)
    return result
