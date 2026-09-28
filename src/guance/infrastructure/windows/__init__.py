"""Windows-specific infrastructure adapters."""
from .certificate import WindowsCertificateManager
from .proxy import InMemoryProxyBackend, WindowsProxyBackend

__all__ = ["InMemoryProxyBackend", "WindowsCertificateManager", "WindowsProxyBackend"]
