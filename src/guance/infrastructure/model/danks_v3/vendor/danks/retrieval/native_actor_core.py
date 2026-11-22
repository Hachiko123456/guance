from __future__ import annotations

"""Public build: native acceleration is not distributed."""


def available() -> bool:
    return False


def module():
    raise RuntimeError("native acceleration is not available in the public build")


# Vendored for Guance danks_v3 from DanKS-Train versions/v3/DanKS/retrieval/native_actor_core.py
# reference commit c618f537c7a83c77b76c3082985e42efff67ec02; original sha256 32c24fcf8ff6b7d72c607be5c5190b92e9c15cbdcd990ab6f24ebda99047b948;
# only absolute import roots were rewritten (DanKS./guandan. -> vendored package roots).
