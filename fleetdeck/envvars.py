"""Environment variables, all named FLEETDECK_*."""

import os


def env(name: str, default: str | None = None) -> str | None:
    """FLEETDECK_<name> if set, else ``default``."""
    return os.environ.get(f"FLEETDECK_{name}") or default
