"""Command-line interface for CamouDesk profile automation and diagnostics."""

import argparse
import json
import sys
from typing import Optional

from camoudesk.core.browser_worker import run_worker
from camoudesk.core.proxy import check_proxy_health
from camoudesk.core.storage import ProfileStore


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="camoudesk", description="CamouDesk Anti-Detect Browser Manager CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("gui", help="Launch the desktop GUI window")

    list_p = sub.add_parser("list", help="List saved profiles")
    list_p.add_argument("--json", action="store_true", help="Output JSON")

    create_p = sub.add_parser("create", help="Create a new persistent profile")
    create_p.add_argument("--name", required=True)
    create_p.add_argument("--os", default="macos", choices=("macos", "windows", "linux"))
    create_p.add_argument("--proxy", default="")
    create_p.add_argument("--timezone", default="America/New_York")
    create_p.add_argument("--locale", default="en-US")
    create_p.add_argument("--url", default="https://browserleaks.com/canvas")

    launch_p = sub.add_parser("launch", help="Launch a saved profile")
    launch_p.add_argument("profile_id")
    launch_p.add_argument("--headless", action="store_true")
    launch_p.add_argument("--url", default=None)

    proxy_p = sub.add_parser("test-proxy", help="Test HTTP/SOCKS5 proxy health")
    proxy_p.add_argument("proxy_url")

    args = parser.parse_args(argv)

    if args.command == "gui":
        from camoudesk.ui.app import main as gui_main
        gui_main()
        return 0

    store = ProfileStore()

    if args.command == "list":
        # Trashed profiles are hidden, as in the app; proxy URLs (may hold passwords) are omitted.
        profiles = [p.to_dict() for p in store.list_profiles() if not p.deleted_at]
        for p in profiles:
            p.pop("proxy", None)
        if args.json:
            print(json.dumps(profiles, indent=2))
        else:
            for p in profiles:
                folder = f"[{p['folder']}] " if p.get("folder") else ""
                print(f"{p['id']:24s}  {p['os']:8s}  {folder}{p['name']}")
        return 0

    if args.command == "create":
        prof = store.create_profile(
            {
                "name": args.name,
                "os": args.os,
                "proxy": args.proxy,
                "timezone": args.timezone,
                "locale": args.locale,
                "startup_url": args.url,
            }
        )
        print(json.dumps(prof.to_dict(), indent=2))
        return 0

    if args.command == "launch":
        return run_worker(args.profile_id, headless=args.headless, url_override=args.url)

    if args.command == "test-proxy":
        res = check_proxy_health(args.proxy_url)
        print(json.dumps(res, indent=2))
        return 0 if res.get("ok") else 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
