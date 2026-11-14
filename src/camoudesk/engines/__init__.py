"""Browser engines behind one interface (engines/base.py). Camoufox is the only engine today."""

from functools import lru_cache

from camoudesk.engines.base import EngineAdapter

DEFAULT_ENGINE = "camoufox"


@lru_cache(maxsize=None)
def get_engine(name: str = DEFAULT_ENGINE) -> EngineAdapter:
    if name == "camoufox":
        from camoudesk.engines.camoufox import CamoufoxEngine

        return CamoufoxEngine()
    raise ValueError(f"Unknown browser engine '{name}'")


__all__ = ["DEFAULT_ENGINE", "EngineAdapter", "get_engine"]
