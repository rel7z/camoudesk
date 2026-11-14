"""Proxy-provider integrations: per-profile sessions and targeting built into proxy credentials.

Each provider is a ``ProviderAdapter``; ``detect(host)`` finds the adapter for a gateway host and
``get(name)`` returns a registered one. Saved proxies store ``provider["name"]`` and go through
the registry, so adding a provider means adding an adapter here, nothing else.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Tuple


class ProviderApiError(RuntimeError):
    """A provider's public API refused or failed a request (message is safe to show)."""


class ProviderAdapter(ABC):
    name: str = ""
    label: str = ""
    products: Dict[str, str] = {}

    @abstractmethod
    def detect(self, host: str) -> Optional[str]:
        """The product for a gateway ``host``, or None when it is not this provider's."""

    @abstractmethod
    def split_password(self, password: str) -> Tuple[str, Dict[str, str]]:
        """A pasted password -> (account base password, options found in it)."""

    @abstractmethod
    def opts_from_params(self, params: Dict[str, str], product: str) -> Dict[str, Any]:
        """Provider options from options pasted in a password (its session is dropped)."""

    @abstractmethod
    def clean_opts(self, raw: Any, *, partial: bool = False) -> Dict[str, Any]:
        """Validated options; ``partial`` keeps only what is set (per-profile overrides)."""

    @abstractmethod
    def effective_opts(self, proxy_opts: Dict[str, Any], profile_opts: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """The saved proxy's options with a profile's overrides on top."""

    @abstractmethod
    def session_id(self, profile_id: str, rotation: int, targeting: str) -> str:
        """A profile's stable session ID (deterministic: every launch path computes the same)."""

    @abstractmethod
    def targeting_key(self, opts: Dict[str, Any]) -> str:
        """What makes a different session (e.g. country/city/session type)."""

    @abstractmethod
    def compose_password(self, base: str, opts: Dict[str, Any], session: str = "") -> str:
        """The password a profile authenticates with."""

    @abstractmethod
    def summary(self, opts: Dict[str, Any]) -> str:
        """Short human label for the UI."""

    @abstractmethod
    def fetch_targets(self, api_key: str, product: str) -> Dict[str, Any]:
        """Targeting lists from the provider's API; raises ProviderApiError."""


_REGISTRY: Dict[str, ProviderAdapter] = {}


def register(adapter: ProviderAdapter) -> ProviderAdapter:
    _REGISTRY[adapter.name] = adapter
    return adapter


def get(name: str) -> ProviderAdapter:
    _load()
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ValueError(f"Unknown proxy provider '{name}'") from None


def detect(host: str) -> Optional[Tuple[ProviderAdapter, str]]:
    """(adapter, product) for a gateway host, or None for a plain proxy."""
    _load()
    for adapter in _REGISTRY.values():
        product = adapter.detect(host)
        if product:
            return adapter, product
    return None


def _load() -> None:
    if not _REGISTRY:
        from camoudesk.core.providers import evomi  # noqa: F401  (registers itself)
