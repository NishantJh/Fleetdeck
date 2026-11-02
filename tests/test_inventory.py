"""An empty or invalidated discovery cache must refresh even just after system boot."""

import pytest

from fleetdeck.inventory import Inventory


@pytest.mark.parametrize("force", [False, True])
async def test_initial_and_invalidated_inventory_refresh_before_cache_interval(monkeypatch, force):
    clock = {"now": 1.0}
    monkeypatch.setattr("fleetdeck.inventory.time.monotonic", lambda: clock["now"])

    class Provider:
        name = "fake"
        calls = 0

        async def discover(self):
            self.calls += 1
            return []

    provider = Provider()
    inventory = Inventory([provider], ttl=600, min_interval=5)
    await inventory.refresh(force=force)
    assert provider.calls == 1

    clock["now"] = 2.0
    await inventory.refresh(force=force)
    assert provider.calls == 1  # An actual recent refresh is still throttled.

    inventory.invalidate()
    await inventory.refresh(force=force)
    assert provider.calls == 2  # Zero is an invalid marker, not a cached timestamp.
