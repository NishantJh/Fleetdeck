"""Environment variables. The FLEETDECK_* names are the current ones; the older HOMELAB_* still work."""

import os


def env(name: str, default: str | None = None) -> str | None:
    """FLEETDECK_<name> if set, else HOMELAB_<name> (the older name), else ``default``."""
    for key in (f"FLEETDECK_{name}", f"HOMELAB_{name}"):
        if os.environ.get(key):
            return os.environ[key]
    return default
