"""AppContext: the stores, process manager, event bus and services, built once per process.

The GUI bridge, the REST API, the CLI, the SDK and the browser worker all go through one context
instead of each assembling its own stores. Services are created lazily on first use.
"""

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from camoudesk.core.events import EventBus
from camoudesk.engines import EngineAdapter, get_engine
from camoudesk.core.provider_store import ProviderStore
from camoudesk.core.proxy_store import ProxyStore
from camoudesk.core.settings_store import SettingsStore
from camoudesk.core.storage import ProfileStore

if TYPE_CHECKING:  # pragma: no cover
    from camoudesk.manager.process_manager import ProcessManager
    from camoudesk.services.archive import ArchiveService
    from camoudesk.services.cookies import CookieService
    from camoudesk.services.identity import IdentityService
    from camoudesk.services.launch import LaunchService
    from camoudesk.services.profiles import ProfileService
    from camoudesk.services.proxies import ProxyService
    from camoudesk.services.settings import SettingsService


@dataclass
class AppContext:
    # Never exposed to the web page through pywebview's js_api (it walks nested objects).
    _serializable = False

    store: ProfileStore
    manager: "ProcessManager"
    proxies: ProxyStore
    settings: SettingsStore
    providers: ProviderStore
    events: EventBus = field(default_factory=EventBus)
    engine: EngineAdapter = field(default_factory=get_engine)

    @classmethod
    def create(
        cls,
        store: Optional[ProfileStore] = None,
        manager: Optional["ProcessManager"] = None,
        proxies: Optional[ProxyStore] = None,
        settings: Optional[SettingsStore] = None,
        app_dir: Optional[Path] = None,
        adopt_running: bool = True,
    ) -> "AppContext":
        from camoudesk.manager.process_manager import ProcessManager

        if store is None:
            store = ProfileStore(app_dir=app_dir) if app_dir else ProfileStore()
        manager = manager or ProcessManager(store=store, adopt_running=adopt_running)
        ctx = cls(
            store=store,
            manager=manager,
            proxies=proxies or manager.proxies,
            settings=settings or SettingsStore(store.app_dir),
            providers=ProviderStore(store.app_dir),
        )
        manager.add_listener(lambda: ctx.events.publish("status"))
        return ctx

    @property
    def app_dir(self) -> Path:
        return self.store.app_dir

    @cached_property
    def profiles_service(self) -> "ProfileService":
        from camoudesk.services.profiles import ProfileService

        return ProfileService(self)

    @cached_property
    def proxy_service(self) -> "ProxyService":
        from camoudesk.services.proxies import ProxyService

        return ProxyService(self)

    @cached_property
    def launch_service(self) -> "LaunchService":
        from camoudesk.services.launch import LaunchService

        return LaunchService(self)

    @cached_property
    def identity_service(self) -> "IdentityService":
        from camoudesk.services.identity import IdentityService

        return IdentityService(self)

    @cached_property
    def cookie_service(self) -> "CookieService":
        from camoudesk.services.cookies import CookieService

        return CookieService(self)

    @cached_property
    def archive_service(self) -> "ArchiveService":
        from camoudesk.services.archive import ArchiveService

        return ArchiveService(self)

    @cached_property
    def settings_service(self) -> "SettingsService":
        from camoudesk.services.settings import SettingsService

        return SettingsService(self)
