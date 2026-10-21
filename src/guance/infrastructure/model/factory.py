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

    def validate_discovery(self, manifest: ModelManifest) -> None:
        """Family-level validation used by catalog discovery.

        Providers that pin artifact identity in `validate` override this with the
        stable-contract subset so future same-contract models stay discoverable
        without code changes; loading still uses the full `validate` gate.
        """
        self.validate(manifest)

    @abstractmethod
    def create_observation_adapter(self) -> Any: ...

    @abstractmethod
    def create_runtime(self, bundle: ModelBundle, *,
                       requested_device: str = "auto") -> Any: ...

    @abstractmethod
    def create(self, bundle: ModelBundle, *,
               requested_device: str = "auto") -> ModelAdvisor: ...


class ModelFactory:
    def __init__(self, registry: ProviderRegistry | None = None, *,
                 requested_device: str = "auto") -> None:
        from .device_policy import normalize_requested_device
        self.registry = registry if registry is not None else default_registry()
        self.requested_device = normalize_requested_device(requested_device)

    def create(self, source: Path | str | ModelBundle, *,
               requested_device: str | None = None) -> ModelAdvisor:
        import inspect
        from .device_policy import normalize_requested_device
        device = (self.requested_device if requested_device is None
                  else normalize_requested_device(requested_device))
        bundle = load_bundle(source, registry=self.registry)
        provider = self.registry.resolve(bundle.manifest)
        # Third-party/test providers may keep the legacy one-argument signature;
        # every in-repository production provider accepts the explicit device.
        if "requested_device" in inspect.signature(provider.create).parameters:
            return provider.create(bundle, requested_device=device)
        return provider.create(bundle)
