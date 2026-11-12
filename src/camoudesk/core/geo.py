"""Deterministic country -> browser locale mapping from Camoufox's bundled CLDR territory data.

Camoufox's own ``StatisticalLocaleSelector`` draws a *random* language weighted by population,
which would change a profile's locale between launches. Here the most widely spoken language
of the territory is always chosen, so an "auto" locale is stable for a given proxy country.
"""

from functools import lru_cache
from typing import Dict, Optional
import xml.etree.ElementTree as ET


@lru_cache(maxsize=1)
def _territory_languages() -> Dict[str, str]:
    from camoufox.locales import get_unicode_info

    root: ET.Element = get_unicode_info()
    best: Dict[str, str] = {}
    for territory in root.iter("territory"):
        code = territory.get("type") or ""
        if len(code) != 2:
            continue
        top_lang, top_pct = "", -1.0
        for lp in territory.findall("languagePopulation"):
            lang = (lp.get("type") or "").split("_")[0]
            try:
                pct = float(lp.get("populationPercent") or 0)
            except ValueError:
                continue
            # Prefer official languages when shares are close, as browsers ship them.
            if lp.get("officialStatus"):
                pct += 0.01
            if lang and pct > top_pct:
                top_lang, top_pct = lang, pct
        if top_lang:
            best[code.upper()] = top_lang
    return best


def locale_for_country(country_code: Optional[str]) -> Optional[str]:
    """``"DE"`` -> ``"de-DE"``; None when the code is unknown."""
    cc = (country_code or "").strip().upper()
    if len(cc) != 2:
        return None
    lang = _territory_languages().get(cc)
    return f"{lang}-{cc}" if lang else None
