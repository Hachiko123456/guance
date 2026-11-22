"""DanKS retrieval prototype."""

from .context import RetrievalContext, build_context
from .ranker import StructuralCandidateRanker

__all__ = ["RetrievalContext", "StructuralCandidateRanker", "build_context"]


# Vendored for Guance danks_v3 from DanKS-Train versions/v3/DanKS/retrieval/__init__.py
# reference commit c618f537c7a83c77b76c3082985e42efff67ec02; original sha256 fa6260eb76fede9491327a39395388b8cce0baedfd7fe3a4ff6d2ecb90763ec8;
# only absolute import roots were rewritten (DanKS./guandan. -> vendored package roots).
