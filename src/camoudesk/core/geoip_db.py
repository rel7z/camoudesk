"""Status and download of Camoufox's GeoIP database (stored in the user cache, outside the app).

With the database present, proxy profiles get geolocation coordinates that match the exit IP
and, when WebRTC is allowed, WebRTC reports the proxy IP instead of the real one.
"""

import time
from typing import Any, Callable, Dict, Optional


def status() -> Dict[str, Any]:
    try:
        from camoufox.geolocation import geoip_allowed, get_mmdb_path

        geoip_allowed()
    except Exception as exc:
        return {"available": False, "installed": False, "error": str(exc)}
    path = get_mmdb_path("ipv4")
    if not path.is_file():
        return {"available": True, "installed": False, "path": str(path)}
    age_days = (time.time() - path.stat().st_mtime) / 86400
    return {"available": True, "installed": True, "path": str(path), "age_days": round(age_days, 1)}


def download(progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, Any]:
    from camoufox.geolocation import download_mmdb

    download_mmdb(progress_callback=progress)
    return status()
