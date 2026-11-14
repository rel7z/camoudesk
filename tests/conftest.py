"""Shared test setup."""

import pytest


@pytest.fixture(autouse=True)
def _no_host_gpu_probe(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Offline tests must not launch Camoufox to probe the GPU (core/host_gpu.py); live tests may."""
    if request.node.get_closest_marker("live_browser") is None:
        monkeypatch.setenv("CAMOUDESK_HOST_GPU", "off")
