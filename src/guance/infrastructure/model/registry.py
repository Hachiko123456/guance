"""Explicit provider registry. No implicit defaults, retries, or runtime substitution."""
from __future__ import annotations

from typing import Protocol

from guance.ports.model import ModelAdvisor
from .errors import ModelBackendError
from .manifest import ModelBundle, ModelManifest


class ProviderFactory(Protocol):
    def validate(self, manifest: ModelManifest) -> None: ...
    def create(self, bundle: ModelBundle) -> ModelAdvisor: ...


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ProviderFactory] = {}

    def register(self, provider: str, factory: ProviderFactory) -> None:
        if provider in self._providers:
            raise ModelBackendError("PROVIDER_DUPLICATE")
        self._providers[provider] = factory

    def resolve(self, manifest: ModelManifest) -> ProviderFactory:
        factory = self._providers.get(manifest.provider)
        if factory is None:
            raise ModelBackendError("PROVIDER_UNKNOWN")
        factory.validate(manifest)
        return factory


def default_registry() -> ProviderRegistry:
    # These modules do not import NumPy, torch or onnx at module import time.
    from .providers.fabledan import FableDanProvider
    from .providers.guandan import GuandanProvider
    from .providers.guandan_marvel_v2 import GuandanMarvelV2Provider
    registry = ProviderRegistry()
    registry.register("fabledan", FableDanProvider())
    guandan = GuandanProvider()
    registry.register("guandan", guandan)
    registry.register("builtin.guandan", guandan)
    registry.register("guandan_marvel_v2", GuandanMarvelV2Provider())
    return registry
