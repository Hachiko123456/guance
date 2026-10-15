"""Infrastructure compatibility aliases for the stable application model errors."""
from __future__ import annotations

from guance.ports.model import MODEL_MESSAGES as MESSAGES
from guance.ports.model import ModelBackendError, ModelErrorCode, public_error

__all__ = ["MESSAGES", "ModelBackendError", "ModelErrorCode", "public_error"]
