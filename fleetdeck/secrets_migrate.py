"""Find plaintext secrets in a parsed config.yaml and swap them for ``${SECRET:name}`` references."""

from __future__ import annotations

import re
from collections.abc import Callable
from copy import deepcopy

from .secrets_store import secret_name

SECRET_KEYS = {"token", "token_secret", "password", "private_key", "backup_password"}
# Notification URLs that are themselves the credential (a Discord webhook, a generic webhook).
SECRET_URL_TYPES = {"discord", "webhook"}
# A value that config expansion would actually replace. A literal "${" in a password (not a reference) is
# still a plaintext secret and must move.
REFERENCE = re.compile(r"\$\{(?:(?:SECRET|FILE):[^}\n]{1,300}|[A-Za-z_][A-Za-z0-9_]*)\}")


def _is_plain(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not REFERENCE.search(value)


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
    """A copy of ``raw`` with every plaintext secret stored through ``put(name, value) -> reference``.

    Two paths can produce the same readable name (names that differ only in punctuation, a truncated long
    name, duplicate machine or notification names). Reusing that name for different values would make one
    secret overwrite the other, so a numeric suffix keeps them apart; the same value reuses one entry."""
    out = deepcopy(raw)
    moved: list[str] = []
    references: dict[str, tuple[str, str]] = {}  # name -> (value, reference)
    for node, key, path in list(_walk(out, (), ())):
        value = node[key]
        base = secret_name(*map(str, path))
        name, suffix = base, 1
        while name in references and references[name][0] != value:
            suffix += 1
            tail = f"_{suffix}"
            name = f"{base[: 120 - len(tail)]}{tail}"
        if name in references:
            node[key] = references[name][1]
        else:
            reference = put(name, value)
            references[name] = (value, reference)
            node[key] = reference
        moved.append(_label(path))
    return out, moved
