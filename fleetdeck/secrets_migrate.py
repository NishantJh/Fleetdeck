"""Find plaintext secrets in a parsed config.yaml and swap them for ``${SECRET:name}`` references."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy

from .secrets_store import secret_name

SECRET_KEYS = {"token", "token_secret", "password", "private_key", "backup_password"}
# Notification URLs that are themselves the credential (a Discord webhook, a generic webhook).
SECRET_URL_TYPES = {"discord", "webhook"}


def _is_plain(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "${" not in value


def _label(path: tuple) -> str:
    return ".".join(str(p) for p in path)


def _walk(node, path: tuple, trail: tuple):
    """Yield (container, key, path-with-names) for every plaintext secret. ``trail`` names list items."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = (*path, key)
            if isinstance(value, str):
                is_url = key == "url" and node.get("type") in SECRET_URL_TYPES
                if (key in SECRET_KEYS or is_url) and _is_plain(value):
                    yield node, key, here
            else:
                yield from _walk(value, here, trail)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            name = item.get("name") if isinstance(item, dict) and isinstance(item.get("name"), str) else i
            yield from _walk(item, (*path, name), trail)


def find_plaintext(raw: dict) -> list[str]:
    """Where plaintext secrets are, as ``proxmox.heimdall.token_secret``. Values are never returned."""
    return [_label(p) for _, _, p in _walk(raw, (), ())]


def migrate(raw: dict, put: Callable[[str, str], str]) -> tuple[dict, list[str]]:
    """A copy of ``raw`` with every plaintext secret stored through ``put(name, value) -> reference``."""
    out = deepcopy(raw)
    moved = []
    for node, key, path in list(_walk(out, (), ())):
        node[key] = put(secret_name(*map(str, path)), node[key])
        moved.append(_label(path))
    return out, moved
