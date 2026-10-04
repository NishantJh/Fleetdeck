"""Package-manager scripts and output parsers.

Every target, whatever the transport (SSH, `pct exec`, QEMU guest agent, Incus exec),
runs the same POSIX ``sh`` script. The script detects the package manager itself, so
the dashboard does not need to know each guest's distro in advance.

Check output on stdout looks like::

    __PM__=apt
    __OS__=Debian GNU/Linux 12 (bookworm)
    __BEGIN__
    <package manager's list of upgradable packages>
    __END__
    __REBOOT__=yes|no|unknown

Refresh logs go to stderr so they never pollute the package list.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from .models import CheckResult, ExecResult, Package

_PRELUDE = r"""
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH
export DEBIAN_FRONTEND=noninteractive
export LC_ALL=C
if command -v apt-get >/dev/null 2>&1; then PM=apt
elif command -v dnf >/dev/null 2>&1; then PM=dnf
elif command -v yum >/dev/null 2>&1; then PM=yum
elif command -v apk >/dev/null 2>&1; then PM=apk
elif command -v pacman >/dev/null 2>&1; then PM=pacman
else PM=unknown; fi
echo "__PM__=$PM"
OS=unknown
if [ -r /etc/os-release ]; then . /etc/os-release; OS="${PRETTY_NAME:-$ID}"; fi
echo "__OS__=$OS"
"""

_REBOOT = r"""
if [ -f /var/run/reboot-required ]; then echo "__REBOOT__=yes"
elif command -v needs-restarting >/dev/null 2>&1; then
  if needs-restarting -r >/dev/null 2>&1; then echo "__REBOOT__=no"; else echo "__REBOOT__=yes"; fi
elif [ "$PM" = apt ]; then echo "__REBOOT__=no"
else echo "__REBOOT__=unknown"; fi
"""

CHECK_SCRIPT = (
    _PRELUDE
    + r"""
rc=0
case "$PM" in
  apt)
    apt-get update -q >&2 || rc=$?
    echo "__BEGIN__"
    apt list --upgradable 2>/dev/null | grep -v '^Listing'
    echo "__END__" ;;
  dnf|yum)
    echo "__BEGIN__"
    $PM -q check-update --refresh 2>/dev/null; r=$?
    echo "__END__"
    # check-update exits 100 when updates exist, 1 on error.
    [ "$r" = 1 ] && rc=1 ;;
  apk)
    apk update -q >&2 || rc=$?
    echo "__BEGIN__"
    apk -u list 2>/dev/null
    echo "__END__" ;;
  pacman)
    echo "__BEGIN__"
    if command -v checkupdates >/dev/null 2>&1; then checkupdates 2>/dev/null
    else pacman -Sy >&2 && pacman -Qu 2>/dev/null; fi
    echo "__END__" ;;
  *)
    echo "No supported package manager found" >&2
    exit 3 ;;
esac
"""
    + _REBOOT
    + "exit $rc\n"
)

UPGRADE_SCRIPT = (
    _PRELUDE
    + r"""
set -e
case "$PM" in
  apt)
    apt-get update -q
    # dist-upgrade (a.k.a. full-upgrade) is what Proxmox and Raspberry Pi OS recommend.
    apt-get -y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold dist-upgrade ;;
  dnf|yum) $PM -y upgrade --refresh ;;
  apk) apk update && apk upgrade ;;
  pacman) pacman -Syu --noconfirm ;;
  *) echo "No supported package manager found" >&2; exit 3 ;;
esac
"""
    + _REBOOT
)


def _markers(stdout: str) -> tuple[dict[str, str], list[str]]:
    meta: dict[str, str] = {}
    body: list[str] = []
    inside = False
    for line in stdout.splitlines():
        if line == "__BEGIN__":
            inside = True
        elif line == "__END__":
            inside = False
        elif inside:
            if line.strip():
                body.append(line)
        elif line.startswith("__") and "=" in line:
            k, v = line.split("=", 1)
            meta[k.strip("_")] = v
    return meta, body


_APT = re.compile(r"^(?P<name>[^/\s]+)/\S+\s+(?P<new>\S+)\s+\S+(?:\s+\[upgradable from: (?P<old>[^\]]+)\])?")
_APK = re.compile(r"^(?P<pkg>\S+)\s.*?(?:\[upgradable from: (?P<old>[^\]]+)\])?$")
_APK_VER = re.compile(r"^(?P<name>.+?)-(?P<ver>\d[^-]*-r\d+)$")


def parse_apt(lines: list[str]) -> list[Package]:
    out = []
    for line in lines:
        m = _APT.match(line)
        if m:
            out.append(Package(name=m["name"], available=m["new"], current=m["old"]))
    return out


def parse_dnf(lines: list[str]) -> list[Package]:
    out = []
    for line in lines:
        if line.startswith(("Obsoleting", "Last metadata", "Security:")) or line.startswith(" "):
            continue
        parts = line.split()
        if len(parts) == 3 and "." in parts[0]:
            name = parts[0].rsplit(".", 1)[0]
            out.append(Package(name=name, available=parts[1]))
    return out


def parse_apk(lines: list[str]) -> list[Package]:
    out = []
    for line in lines:
        m = _APK.match(line)
        if not m:
            continue
        nv = _APK_VER.match(m["pkg"])
        if not nv:
            continue
        current = None
        if m["old"]:
            ov = _APK_VER.match(m["old"])
            current = ov["ver"] if ov else m["old"]
        out.append(Package(name=nv["name"], available=nv["ver"], current=current))
    return out


def parse_pacman(lines: list[str]) -> list[Package]:
    out = []
    for line in lines:
        parts = line.split()
        if len(parts) >= 4 and parts[2] == "->":
            out.append(Package(name=parts[0], current=parts[1], available=parts[3]))
    return out


PARSERS = {
    "apt": parse_apt,
    "dnf": parse_dnf,
    "yum": parse_dnf,
    "apk": parse_apk,
    "pacman": parse_pacman,
}


def _reboot(meta: dict[str, str]) -> bool | None:
    return {"yes": True, "no": False}.get(meta.get("REBOOT", ""))


def parse_check(result: ExecResult) -> CheckResult:
    meta, body = _markers(result.stdout)
    pm = meta.get("PM")
    now = datetime.now(UTC)
    error = None
    if result.rc != 0:
        tail = "\n".join((result.stderr or result.stdout).strip().splitlines()[-5:])
        error = f"exit code {result.rc}: {tail}" if tail else f"exit code {result.rc}"
    parser = PARSERS.get(pm or "")
    packages = parser(body) if parser else []
    if parser is None and error is None:
        error = f"unsupported package manager: {pm or 'unknown'}"
    return CheckResult(
        package_manager=pm,
        os=meta.get("OS"),
        packages=packages,
        reboot_required=_reboot(meta),
        checked_at=now,
        error=error,
    )


def parse_reboot(stdout: str) -> bool | None:
    meta, _ = _markers(stdout)
    return _reboot(meta)
