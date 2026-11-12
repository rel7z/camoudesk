"""Camoufox launch configuration shared by the direct launcher and the Playwright worker.

Direct mode computes the full Camoufox identity in-process with ``camoufox.utils.launch_options``
and starts the browser binary itself. Camoufox reads its fingerprint config (``CAMOU_CONFIG_*``)
and Firefox prefs (``CAMOU_PREFS_*``) from the environment at startup via camoufox.cfg, so no
Playwright driver or long-lived Python worker is needed for interactive browsing.
"""

from dataclasses import dataclass
import ipaddress
import os
from typing import Any, Dict, List, Mapping, Optional

from camoudesk.core.fingerprint import ensure_profile_fingerprint
from camoudesk.models.profile import Profile, ProxyConfig


# Timezone/locale value meaning "match the network": the proxy's exit location when one is
# configured (resolved by the caller), otherwise the host's own settings (no spoofing).
AUTO = "auto"

# Host environment a browser actually needs. Everything else in the GUI's environment (API keys,
# tokens, SSH agent sockets, proxy variables from a developer shell) is withheld from browsers.
ENV_ALLOWLIST = (
    "HOME",
    "PATH",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "USER",
    "LOGNAME",
    "SHELL",
    "__CF_USER_TEXT_ENCODING",
)


def minimal_env(source: Optional[Mapping[str, str]] = None, extra_prefixes: tuple = ()) -> Dict[str, str]:
    """The allowlisted subset of ``source`` (default: os.environ), plus keys with ``extra_prefixes``."""
    src = os.environ if source is None else source
    return {
        k: str(v)
        for k, v in src.items()
        if k in ENV_ALLOWLIST or any(k.startswith(p) for p in extra_prefixes)
    }


def _browser_env(launch_env: Mapping[str, Any]) -> Dict[str, str]:
    """Keep only what Camoufox added (CAMOU_CONFIG_*, CAMOU_PREFS_*, ...) plus the allowlist.

    ``launch_options`` returns os.environ merged with its own variables; anything whose value
    differs from the host environment was set by Camoufox and is required.
    """
    host = os.environ
    env = minimal_env(host)
    for k, v in launch_env.items():
        if host.get(k) != str(v):
            env[str(k)] = str(v)
    return env


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
    if timezone and timezone != AUTO:
        config["timezone"] = timezone

    kwargs: Dict[str, Any] = {
        "persistent_context": True,
        "user_data_dir": user_data_dir,
        "fingerprint": saved_fingerprint,
        "i_know_what_im_doing": True,
        "os": profile_os,
        "block_webrtc": block_webrtc,
        "humanize": humanize,
        # Avoid Camoufox nested host_gpu() subprocess probe which can deadlock Juggler pipes on macOS.
        "canvas_noise": canvas_noise,
        "headless": headless,
    }
    if locale and locale != AUTO:
        kwargs["locale"] = locale
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


@dataclass(frozen=True)
class Network:
    """A profile's effective network identity, resolved from its saved or inline proxy."""

    proxy: ProxyConfig  # what the browser routes through (may carry credentials)
    source: str = "none"  # "none" | "saved" | "inline"
    proxy_id: str = ""
    exit_ip: str = ""
    country_code: str = ""
    timezone: str = ""  # effective timezone to spoof; "" = don't spoof (host's own)
    locale: str = ""  # effective locale; "" = Camoufox/host default
    needs_check: bool = False  # proxy set + auto tz/locale requested, but no exit data yet
    missing_saved_proxy: bool = False  # proxy_id points at a deleted saved proxy


def resolve_network(profile: Profile, proxies: Any = None) -> Network:
    """Resolve proxy, "auto" timezone/locale and exit IP for a launch.

    "auto" follows the proxy's last check (exit timezone, the country's main language); with no
    proxy it means the host's own settings, which match the host's real IP.
    """
    from camoudesk.core.geo import locale_for_country

    proxy, source, exit_ip, tz, cc = ProxyConfig(), "none", "", "", ""
    if profile.proxy_id:
        saved = proxies.get(profile.proxy_id) if proxies is not None else None
        if saved is None:
            return Network(proxy=ProxyConfig(), source="saved", proxy_id=profile.proxy_id, missing_saved_proxy=True)
        proxy, source = saved.to_config(), "saved"
        chk = saved.last_check
        if chk.ok:
            exit_ip, tz, cc = chk.ip, chk.timezone, chk.country_code
    elif profile.proxy:
        proxy, source = ProxyConfig.from_url(profile.proxy), "inline"
        exit_ip, tz, cc = profile.proxy_exit_ip, profile.proxy_timezone, profile.proxy_country

    has_proxy = proxy.is_configured
    needs_check = False
    if profile.timezone == AUTO:
        timezone = tz if has_proxy else ""
        needs_check |= has_proxy and not tz
    else:
        timezone = profile.timezone
    if profile.locale == AUTO:
        locale = (locale_for_country(cc) or "") if has_proxy else ""
        needs_check |= has_proxy and not cc
    else:
        locale = profile.locale
    return Network(
        proxy=proxy,
        source=source,
        proxy_id=profile.proxy_id if source == "saved" else "",
        exit_ip=exit_ip if has_proxy else "",
        country_code=cc if has_proxy else "",
        timezone=timezone,
        locale=locale,
        needs_check=needs_check,
    )


def has_proxy_credentials(profile: Profile, proxies: Any = None) -> bool:
    """Whether a direct launch needs the loopback ProxyForwarder (Firefox cannot authenticate itself)."""
    return resolve_network(profile, proxies).proxy.has_credentials


def resolve_geoip(profile: Profile, network: Optional[Network] = None) -> Optional[str]:
    """Return the proxy exit IP when the optional camoufox[geoip] database can locate it."""
    net = network or resolve_network(profile)
    if not net.proxy.is_configured or not net.exit_ip:
        return None
    try:
        from camoufox.geolocation import geoip_allowed, get_geolocation, get_mmdb_path

        geoip_allowed()
        version = "ipv6" if ipaddress.ip_address(net.exit_ip).version == 6 else "ipv4"
        if not get_mmdb_path(version).is_file():
            return None
        get_geolocation(net.exit_ip)
    except Exception:
        return None
    return net.exit_ip


def build_launch_spec(
    profile: Profile,
    store: Any,
    headless: bool = False,
    url_override: Optional[str] = None,
    local_proxy: Optional[ProxyConfig] = None,
    network: Optional[Network] = None,
) -> LaunchSpec:
    """Compute the executable, arguments, and environment to start Camoufox without Playwright.

    For a proxy with credentials, pass ``local_proxy`` (a running ProxyForwarder's
    ``local_proxy``); Firefox is pointed at it and never sees the credentials.
    """
    from camoufox.utils import launch_options

    net = network or resolve_network(profile, _proxy_store_for(store))
    if net.missing_saved_proxy:
        raise ValueError("This profile's saved proxy was deleted; choose another proxy or Direct.")
    if net.proxy.has_credentials:
        if local_proxy is None or local_proxy.has_credentials:
            raise ValueError("A proxy with credentials needs a running local forwarder for direct launch.")
        browser_proxy = local_proxy
    else:
        browser_proxy = net.proxy

    kwargs, geoip = profile_camoufox_kwargs(profile, store, net, headless)
    kwargs.pop("persistent_context", None)
    ff_dir = kwargs.pop("user_data_dir")
    # The upstream proxy stays in kwargs so Camoufox derives the same identity as a worker launch
    # (it only inspects it, e.g. for IPv6 handling); the browser itself is routed by these prefs.
    prefs = browser_proxy.to_firefox_prefs()

    # Same pin_cpu_cores default Camoufox's own sync launcher applies.
    opts = launch_options(pin_cpu_cores=False, firefox_user_prefs=prefs or None, **kwargs)

    args = [str(a) for a in (opts.get("args") or [])]
    args += ["-profile", str(ff_dir), "-no-remote"]
    args.append("-headless" if headless else "-foreground")
    # Each extra command-line URL opens in its own tab.
    args += [url_override.strip()] if url_override and url_override.strip() else list(profile.start_urls)

    env = _browser_env(opts.get("env") or {})
    return LaunchSpec(executable=str(opts["executable_path"]), args=args, env=env, geoip=geoip)


def _proxy_store_for(store: Any) -> Any:
    from camoudesk.core.proxy_store import ProxyStore

    return ProxyStore(store.app_dir)


def profile_camoufox_kwargs(
    profile: Profile, store: Any, network: Network, headless: bool
) -> "tuple[Dict[str, Any], Optional[str]]":
    """Camoufox(persistent_context=True, ...) kwargs for a profile; shared by both launch modes."""
    pdir = store.profile_dir(profile.id)
    ff_dir = store.firefox_data_dir(profile.id)
    ff_dir.mkdir(parents=True, exist_ok=True)
    saved_fp, _ = ensure_profile_fingerprint(pdir, profile.os)
    geoip = resolve_geoip(profile, network)
    kwargs = build_camoufox_kwargs(
        profile_os=profile.os,
        user_data_dir=str(ff_dir),
        saved_fingerprint=saved_fp,
        proxy_url=network.proxy.raw_url if network.proxy.is_configured else "",
        timezone=network.timezone,
        locale=network.locale,
        block_webrtc=profile.block_webrtc,
        humanize=profile.humanize,
        headless=headless,
        geoip=geoip,
    )
    return kwargs, geoip
