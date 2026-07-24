"""Multi-site adapters for capture and download."""

from __future__ import annotations

from sites.alphaporno import AlphaPornoAdapter
from sites.beeg import BeegAdapter
from sites.drtuber import DrTuberAdapter
from sites.empflix import EMPFlixAdapter
from sites.eporner import DEFAULT_URL as EPORNER_DEFAULT_URL
from sites.eporner import EpornerAdapter
from sites.eroprofile import EroProfileAdapter
from sites.hanime import HanimeAdapter
from sites.hypnotube import HypnoTubeAdapter
from sites.jable import JableAdapter
from sites.missav import MissAVAdapter
from sites.porn91 import Porn91Adapter
from sites.pornhub import PornhubAdapter
from sites.redtube import RedTubeAdapter
from sites.registry import (
    all_adapters,
    default_adapter,
    get_adapter_by_name,
    get_adapter_for_url,
    register,
)
from sites.resolve import resolve_playable
from sites.spankbang import SpankBangAdapter
from sites.tube8 import Tube8Adapter
from sites.urls import extract_urls_from_target, folder_tag_for_target
from sites.xhamster import XHamsterAdapter
from sites.xnxx import XNXXAdapter
from sites.xvideos import XVideosAdapter
from sites.youporn import YouPornAdapter

_REGISTERED = False


def ensure_registered() -> None:
    """Idempotent registration of built-in adapters."""
    global _REGISTERED
    if _REGISTERED:
        return
    # Default search site first
    register(EpornerAdapter(), default=True)
    register(PornhubAdapter())
    register(XVideosAdapter())
    register(XHamsterAdapter())
    register(XNXXAdapter())
    register(SpankBangAdapter())
    # Tier 2 native
    register(BeegAdapter())
    register(DrTuberAdapter())
    register(RedTubeAdapter())
    register(YouPornAdapter())
    register(Tube8Adapter())
    register(AlphaPornoAdapter())
    register(EMPFlixAdapter())
    register(EroProfileAdapter())
    # Tier 3
    register(MissAVAdapter())
    register(JableAdapter())
    register(Porn91Adapter())
    register(HanimeAdapter())
    register(HypnoTubeAdapter())
    _REGISTERED = True


ensure_registered()

__all__ = [
    "EPORNER_DEFAULT_URL",
    "all_adapters",
    "default_adapter",
    "ensure_registered",
    "extract_urls_from_target",
    "folder_tag_for_target",
    "get_adapter_by_name",
    "get_adapter_for_url",
    "register",
    "resolve_playable",
]
