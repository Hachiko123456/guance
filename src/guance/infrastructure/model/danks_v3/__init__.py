"""DanKS V3 top10 selector provider core (phases 2-4 land here).

Phase 2 scope: the vendored reference subset (``vendor/``), the strict CPU
loader (:mod:`guance.infrastructure.model.danks_v3.loader`, D01) and the
offline inference core (:mod:`guance.infrastructure.model.danks_v3.core`,
D02).  Provider wiring, adapters and IPC come in later phases; nothing in
this package touches the game state, capture or UI layers.
"""
# Heavy imports stay out of the package init: default_registry() imports the
# provider module through this package, and the architecture guard forbids an
# eager torch/numpy load.  Use guance.infrastructure.model.danks_v3.loader
# directly (or the provider) once a torch runtime is actually needed.
from .source_identity import (
    REFERENCE_COMMIT, VENDOR_MANIFEST_PATH, verify_vendor_manifest,
)

__all__ = [
    "REFERENCE_COMMIT", "VENDOR_MANIFEST_PATH", "verify_vendor_manifest",
]
