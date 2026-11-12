"""Camoufox launch configuration shared by the direct launcher and the Playwright worker.

Direct mode computes the full Camoufox identity in-process with ``camoufox.utils.launch_options``
and starts the browser binary itself. Camoufox reads its fingerprint config (``CAMOU_CONFIG_*``)
and Firefox prefs (``CAMOU_PREFS_*``) from the environment at startup via camoufox.cfg, so no
Playwright driver or long-lived Python worker is needed for interactive browsing.
"""

from dataclasses import dataclass
import ipaddress
from typing import Any, Dict, List, Optional

from camoudesk.core.fingerprint import ensure_profile_fingerprint
from camoudesk.models.profile import Profile, ProxyConfig


@dataclass(frozen=True)
class LaunchSpec:
    executable: str
    args: List[str]
    env: Dict[str, str]
    geoip: Optional[str] = None


def build_camoufox_kwargs(
    profile_os: str,
    user_data_dir: str,
    saved_fingerprint: Dict[str, Any],
    proxy_url: str = "",
    timezone: str = "",
    locale: str = "en-US",
    block_webrtc: bool = True,
    humanize: bool = True,
    canvas_noise: bool = True,
    headless: bool = False,
    geoip: Optional[str] = None,
) -> Dict[str, Any]:
    """Build verified keyword arguments for Camoufox(persistent_context=True, ...)."""
    config: Dict[str, Any] = {}
    if timezone:
        config["timezone"] = timezone

    kwargs: Dict[str, Any] = {
        "persistent_context": True,
        "user_data_dir": user_data_dir,
        "fingerprint": saved_fingerprint,
        "i_know_what_im_doing": True,
        "os": profile_os,
        "locale": locale or "en-US",
        "block_webrtc": block_webrtc,
        "humanize": humanize,
        # Avoid Camoufox nested host_gpu() subprocess probe which can deadlock Juggler pipes on macOS.
        "canvas_noise": canvas_noise,
        "headless": headless,
    }
    if config:
        kwargs["config"] = config
    if geoip:
        # Explicit timezone/locale above still win (Camoufox uses setdefault for those keys);
        # geoip adds coordinates and, when WebRTC is allowed, the exit IP for WebRTC.
        kwargs["geoip"] = geoip

    proxy_cfg = ProxyConfig.from_url(proxy_url).to_playwright_proxy() if proxy_url else None
    if proxy_cfg:
        kwargs["proxy"] = proxy_cfg

    return kwargs


def has_proxy_credentials(profile: Profile) -> bool:
    """Whether a direct launch needs the loopback ProxyForwarder (Firefox cannot authenticate itself)."""
    return bool(profile.proxy) and ProxyConfig.from_url(profile.proxy).has_credentials


def resolve_geoip(profile: Profile) -> Optional[str]:
    """Return the cached proxy exit IP when the optional camoufox[geoip] database can locate it."""
    if not profile.proxy or not profile.proxy_exit_ip:
        return None
    try:
        from camoufox.geolocation import geoip_allowed, get_geolocation, get_mmdb_path

        geoip_allowed()
        version = "ipv6" if ipaddress.ip_address(profile.proxy_exit_ip).version == 6 else "ipv4"
        if not get_mmdb_path(version).is_file():
            return None
        get_geolocation(profile.proxy_exit_ip)
    except Exception:
        return None
    return profile.proxy_exit_ip


def build_launch_spec(
    profile: Profile,
    store: Any,
    headless: bool = False,
    url_override: Optional[str] = None,
    local_proxy: Optional[ProxyConfig] = None,
) -> LaunchSpec:
    """Compute the executable, arguments, and environment to start Camoufox without Playwright.

    For a proxy with credentials, pass ``local_proxy`` (a running ProxyForwarder's
    ``local_proxy``); Firefox is pointed at it and never sees the credentials.
    """
    from camoufox.utils import launch_options

    proxy_cfg = ProxyConfig.from_url(profile.proxy) if profile.proxy else ProxyConfig()
    if proxy_cfg.has_credentials:
        if local_proxy is None or local_proxy.has_credentials:
            raise ValueError("A proxy with credentials needs a running local forwarder for direct launch.")
        browser_proxy = local_proxy
    else:
        browser_proxy = proxy_cfg

    pdir = store.profile_dir(profile.id)
    ff_dir = store.firefox_data_dir(profile.id)
    ff_dir.mkdir(parents=True, exist_ok=True)
    saved_fp, _ = ensure_profile_fingerprint(pdir, profile.os)
    geoip = resolve_geoip(profile)

    kwargs = build_camoufox_kwargs(
        profile_os=profile.os,
        user_data_dir=str(ff_dir),
        saved_fingerprint=saved_fp,
        proxy_url=profile.proxy,
        timezone=profile.timezone,
        locale=profile.locale,
        block_webrtc=profile.block_webrtc,
        humanize=profile.humanize,
        headless=headless,
        geoip=geoip,
    )
    kwargs.pop("persistent_context", None)
    kwargs.pop("user_data_dir", None)
    # The upstream proxy stays in kwargs so Camoufox derives the same identity as a worker launch
    # (it only inspects it, e.g. for IPv6 handling); the browser itself is routed by these prefs.
    prefs = browser_proxy.to_firefox_prefs()

    # Same pin_cpu_cores default Camoufox's own sync launcher applies.
    opts = launch_options(pin_cpu_cores=False, firefox_user_prefs=prefs or None, **kwargs)

    args = [str(a) for a in (opts.get("args") or [])]
    args += ["-profile", str(ff_dir), "-no-remote"]
    args.append("-headless" if headless else "-foreground")
    target_url = (url_override or profile.startup_url or "").strip()
    if target_url:
        args.append(target_url)

    env = {str(k): str(v) for k, v in (opts.get("env") or {}).items()}
    return LaunchSpec(executable=str(opts["executable_path"]), args=args, env=env, geoip=geoip)
