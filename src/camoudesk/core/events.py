"""In-process event bus: services publish what changed; the UI host and others subscribe.

Topics: ``status`` (a browser started/stopped/exited/warned), ``proxies`` (saved proxies or their
checks changed), ``theme`` (Settings → Appearance), ``settings`` (any settings change).
Handlers run on the publishing thread and must not raise; errors are swallowed so one listener
cannot break another (or the service that published).
"""

import threading
from typing import Any, Callable, Dict, List

Handler = Callable[..., None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: Dict[str, List[Handler]] = {}
        self._lock = threading.Lock()

    def subscribe(self, topic: str, handler: Handler) -> Callable[[], None]:
        """Register ``handler`` for ``topic``; returns a function that unsubscribes it."""
        with self._lock:
            self._handlers.setdefault(topic, []).append(handler)

        def unsubscribe() -> None:
            with self._lock:
                if handler in self._handlers.get(topic, []):
                    self._handlers[topic].remove(handler)

        return unsubscribe

    def publish(self, topic: str, **data: Any) -> None:
        with self._lock:
            handlers = list(self._handlers.get(topic, []))
        for handler in handlers:
            try:
                handler(**data)
            except Exception:
                pass
