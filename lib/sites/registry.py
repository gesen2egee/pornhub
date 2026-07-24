"""Site adapter registry."""

from __future__ import annotations

from sites.base import SiteAdapter
from sites.generic import GenericAdapter

_REGISTRY: list[SiteAdapter] = []
_DEFAULT: SiteAdapter | None = None
_GENERIC = GenericAdapter()


def clear_registry() -> None:
    """Test helper: wipe registered adapters."""
    global _DEFAULT
    _REGISTRY.clear()
    _DEFAULT = None


def register(adapter: SiteAdapter, *, default: bool = False) -> SiteAdapter:
    _REGISTRY.append(adapter)
    global _DEFAULT
    if default or _DEFAULT is None:
        _DEFAULT = adapter
    return adapter


def all_adapters() -> list[SiteAdapter]:
    return list(_REGISTRY)


def default_adapter() -> SiteAdapter:
    if _DEFAULT is None:
        return _GENERIC
    return _DEFAULT


def get_adapter_for_url(url: str | None) -> SiteAdapter:
    if not url:
        return default_adapter()
    for adapter in _REGISTRY:
        if adapter.match_url(url):
            return adapter
    return _GENERIC


def get_adapter_by_name(name: str) -> SiteAdapter | None:
    for adapter in _REGISTRY:
        if adapter.name == name:
            return adapter
    return None
