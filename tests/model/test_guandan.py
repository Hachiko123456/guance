"""MBA-P0-001, MBA-P1-004: unavailable production; explicit synthetic runtime only."""
from dataclasses import replace

import pytest

from guance.infrastructure.model.errors import ModelBackendError
from guance.infrastructure.model.factory import ModelFactory
from guance.infrastructure.model.manifest import load_bundle
from guance.infrastructure.model.providers.guandan import (
    COMMIT, PASS, GuandanDecisionState, GuandanProvider, GuandanPublicObservationAdapter,
    GuandanTokenActionDecoder, LegalMaskBeamSearch,
)
from guance.infrastructure.model.registry import ProviderRegistry
from guance.contracts import PublicAction


class FakeRuntime:
    """Test-only scoring stub; deliberately ranks an illegal token highest."""
    def __init__(self):
        self.loaded = False
        self.closed = False
        self.calls = []
    def load(self, artifact, manifest):
        self.loaded = True
    def score_tokens(self, state, prefix, legal_mask):
        self.calls.append((prefix, legal_mask))
        return {"a": 0.8, "b": 0.2, COMMIT: 0.0, PASS: -0.5, "ILLEGAL": 1000.0}
    def close(self):
        self.closed = True


class SyntheticAdapter(GuandanPublicObservationAdapter):
    def adapt(self, view):
        def legal(prefix):
            if not prefix:
                return ("a", "b") if view.free_lead else ("a", "b", PASS)
            if len(prefix) == 1 and prefix[0] in ("a", "b"):
                return (COMMIT,)
            return ()
        return GuandanDecisionState(view, "synthetic-select", (), "synthetic-no-previous-result", legal,
                                     lambda cards: not cards and not view.free_lead or
                                     cards in ((0x11,), (0x13,)))


def bundle_for_guandan(manifest_path):
    bundle = load_bundle(manifest_path)
    return replace(bundle, manifest=replace(bundle.manifest, provider="guandan", backend="guandan",
        runtime="unavailable", artifact_format="pt", rules_version="GD-RULES-0.1",
        action_version="GD-ACTION-0.1", environment_version="GD-ENV-0.1", encoding_version="GD-ENCODING-0.1"))


def test_production_guandan_is_explicitly_unavailable_without_fallback(manifest_path, view):
    bundle = bundle_for_guandan(manifest_path)
    advisor = ModelFactory().create(bundle)
    with pytest.raises(ModelBackendError) as exc:
        advisor.load()
    assert exc.value.code == "MODEL_UNAVAILABLE"
    with pytest.raises(ModelBackendError) as exc:
        advisor.recommend(view, "request")
    assert exc.value.code == "MODEL_NOT_LOADED"


def test_current_public_view_cannot_be_fabricated_into_a04_a05(view):
    with pytest.raises(ModelBackendError) as exc:
        GuandanPublicObservationAdapter().adapt(view)
    assert exc.value.code == "GUANDAN_STATE_UNAVAILABLE"
    with pytest.raises(ModelBackendError):
        GuandanProvider(runtime_factory=lambda bundle: FakeRuntime())


def test_explicit_fake_runtime_search_decoder_and_top_n(manifest_path, view):
    runtime = FakeRuntime()
    provider = GuandanProvider(runtime_factory=lambda bundle: runtime,
        observation_adapter=SyntheticAdapter(),
        decoder=GuandanTokenActionDecoder({"a": 0x11, "b": 0x13}), test_only=True)
    registry = ProviderRegistry()
    registry.register("guandan", provider)
    advisor = ModelFactory(registry).create(bundle_for_guandan(manifest_path))
    assert advisor.load()["test_only"] is True
    assert runtime.loaded
    result = advisor.recommend(view, "request")
    assert [c.cards for c in result.candidates] == [(0x11,), (0x13,)]
    assert [c.score for c in result.candidates] == [0.8, 0.2]
    assert result.metadata == {"test_only": True}
    assert all(c.score_kind == "guandan_token_score" for c in result.candidates)
    state = SyntheticAdapter().adapt(view)
    assert len(LegalMaskBeamSearch().search(runtime, state, top_n=1)) == 1
    assert all("ILLEGAL" not in mask for _, mask in runtime.calls)
    advisor.close()
    assert runtime.closed


def test_pass_is_complete_action_commit_is_not_pass(view):
    lead = PublicAction(1, 1, "play", (0x22,), 1, 0, 0)
    follow = replace(view, free_lead=False, lead_action=lead, public_history=(lead,))
    state = SyntheticAdapter().adapt(follow)
    decoder = GuandanTokenActionDecoder({"a": 0x11, "b": 0x13})
    assert decoder.decode((PASS,), state, -0.5).cards == ()
    assert decoder.decode(("a", COMMIT), state, 0.8).cards == (0x11,)
    for tokens in ((COMMIT,), ("a",), (PASS, COMMIT), ("a", PASS), ("a", COMMIT, "b"), ("x", COMMIT)):
        with pytest.raises(ModelBackendError) as exc:
            decoder.decode(tokens, state, 0.0)
        assert exc.value.code == "GUANDAN_TOKEN_INVALID"
    with pytest.raises(ModelBackendError):
        decoder.decode((PASS,), SyntheticAdapter().adapt(view), 0.0)


def test_legal_mask_does_not_replace_whole_action_legality_check(view):
    state = SyntheticAdapter().adapt(view)
    state = replace(state, validate_action=lambda cards: False)
    with pytest.raises(ModelBackendError):
        GuandanTokenActionDecoder({"a": 0x11}).decode(("a", COMMIT), state, 0.0)
    state = replace(state, legal_tokens=lambda prefix: ("a", COMMIT))
    with pytest.raises(ModelBackendError):
        GuandanTokenActionDecoder({"a": 0x22}).decode(("a", COMMIT), state, 0.0)


@pytest.mark.parametrize("bad_mask", [(), ("a", "a"), ["a"], (123,)])
def test_bad_legal_masks_fail_closed(view, bad_mask):
    state = replace(SyntheticAdapter().adapt(view), legal_tokens=lambda prefix: bad_mask)
    with pytest.raises(ModelBackendError) as exc:
        LegalMaskBeamSearch().search(FakeRuntime(), state, 3)
    assert exc.value.code == "GUANDAN_TOKEN_INVALID"


def test_search_must_terminate_with_complete_action(view):
    state = replace(SyntheticAdapter().adapt(view), legal_tokens=lambda prefix: ("a",))
    with pytest.raises(ModelBackendError):
        LegalMaskBeamSearch(max_steps=2).search(FakeRuntime(), state, 3)



def test_fake_runtime_is_only_accepted_by_explicit_test_provider(manifest_path, view):
    bundle = bundle_for_guandan(manifest_path)
    bundle = replace(bundle, manifest=replace(bundle.manifest, runtime="test", artifact_format="test"))
    with pytest.raises(ModelBackendError) as exc:
        ModelFactory().create(bundle)
    assert exc.value.code == "MODEL_UNAVAILABLE"
    runtime = FakeRuntime()
    provider = GuandanProvider(runtime_factory=lambda bundle: runtime,
        observation_adapter=SyntheticAdapter(),
        decoder=GuandanTokenActionDecoder({"a": 0x11}), test_only=True)
    registry = ProviderRegistry()
    registry.register("guandan", provider)
    advisor = ModelFactory(registry).create(bundle)
    assert advisor.load()["test_only"] is True
    advisor.close()


def test_user_manifest_guandan_identifiers_are_recognized_but_never_production_ready(manifest_path):
    bundle = bundle_for_guandan(manifest_path)
    bundle = replace(bundle, manifest=replace(bundle.manifest,
        provider="builtin.guandan", artifact_format="torch_checkpoint", input_contract="guandan-public-v1",
        output_contract="canonical-advice-v1", runtime="torch"))
    advisor = ModelFactory().create(bundle)
    with pytest.raises(ModelBackendError) as exc:
        advisor.load()
    assert exc.value.code == "MODEL_UNAVAILABLE"
    advisor.close()


@pytest.mark.parametrize("field", ["rules_version", "action_version", "environment_version", "encoding_version"])
def test_guandan_compatibility_checks_each_gd_version(manifest_path, field):
    bundle = bundle_for_guandan(manifest_path)
    bundle = replace(bundle, manifest=replace(bundle.manifest, **{field: "GD-UNSUPPORTED-9.9"}))
    with pytest.raises(ModelBackendError) as exc:
        ModelFactory().create(bundle)
    assert exc.value.code == "MODEL_VERSION_UNSUPPORTED"


def test_requested_public_runtime_decoder_names():
    from guance.infrastructure.model.providers.guandan import (
        GuandanModelRuntime, GuandanRuntime, TokenActionDecoder,
    )
    assert GuandanModelRuntime is GuandanRuntime
    assert TokenActionDecoder is GuandanTokenActionDecoder


def test_test_runtime_metadata_records_provider_and_canonical_wire_contract(manifest_path):
    runtime = FakeRuntime()
    bundle = bundle_for_guandan(manifest_path)
    bundle = replace(bundle, manifest=replace(bundle.manifest, provider="builtin.guandan",
        input_contract="guandan-public-v1", artifact_format="torch_checkpoint"))
    provider = GuandanProvider(runtime_factory=lambda bundle: runtime,
        observation_adapter=SyntheticAdapter(), decoder=GuandanTokenActionDecoder({"a": 0x11, "b": 0x13}),
        test_only=True)
    registry = ProviderRegistry()
    registry.register("builtin.guandan", provider)
    advisor = ModelFactory(registry).create(bundle)
    metadata = advisor.load()
    assert metadata["provider"] == "builtin.guandan"
    assert metadata["input_contract"] == "guandan-public-v1"
    assert metadata["wire_input_contract"] == "CanonicalPublicGameView.v1"
    assert metadata["test_only"] is True
    advisor.close()
