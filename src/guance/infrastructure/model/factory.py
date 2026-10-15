"""Abstract factory for matched observation adapter, runtime, and advisor families."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from guance.ports.model import ModelAdvisor
from .manifest import ModelBundle, ModelManifest, load_bundle
from .registry import ProviderRegistry, default_registry


class AbstractProviderFactory(ABC):
    @abstractmethod
    def validate(self, manifest: ModelManifest) -> None: ...

    @abstractmethod
    def create_observation_adapter(self) -> Any: ...

    @abstractmethod
    def create_runtime(self, bundle: ModelBundle) -> Any: ...

    @abstractmethod
    def create(self, bundle: ModelBundle) -> ModelAdvisor: ...


class ModelFactory:
    def __init__(self, registry: ProviderRegistry | None = None) -> None:
        self.registry = registry if registry is not None else default_registry()

    def create(self, source: Path | str | ModelBundle) -> ModelAdvisor:
        bundle = load_bundle(source, registry=self.registry)
        provider = self.registry.resolve(bundle.manifest)
        return provider.create(bundle)
