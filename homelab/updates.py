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
export DEBIAN_FRONTEND=noninteractive APT_LISTCHANGES_FRONTEND=none
# Ubuntu's needrestart would otherwise try to ask which services to restart.
export NEEDRESTART_MODE=a
export LC_ALL=C
if command -v apt-get >/dev/null 2>&1; then PM=apt
elif command -v dnf >/dev/null 2>&1; then PM=dnf
elif command -v yum >/dev/null 2>&1; then PM=yum
elif command -v apk >/dev/null 2>&1; then PM=apk
elif command -v pacman >/dev/null 2>&1; then PM=pacman
else PM=unknown; fi
echo "__PM__=$PM"
# Read os-release in a subshell so its variables can't clobber ours.
OS=$( [ -r /etc/os-release ] && . /etc/os-release && echo "${PRETTY_NAME:-$ID}" )
echo "__OS__=${OS:-unknown}"
# Wait for unattended-upgrades & co. instead of failing on the dpkg lock.
APT_OPTS="-o DPkg::Lock::Timeout=300"
"""

# Reboot detection. Debian and Proxmox don't create /var/run/reboot-required for kernel
# updates, so also compare the running kernel with the newest installed one of the same
# flavour (the part after the last "-": pve, amd64, generic, v8, lts...). Containers have no
# kernel of their own (no /lib/modules), so they never report one.
REBOOT_SCRIPT = r"""
MODULES_DIR=${MODULES_DIR:-/lib/modules}
running=$(uname -r)
if printf '1\n' | sort -V >/dev/null 2>&1; then SORT="sort -V"; else SORT=sort; fi
latest=$(ls -1 "$MODULES_DIR" 2>/dev/null | grep -- "-${running##*-}\$" | $SORT | tail -n 1)
if [ -f /var/run/reboot-required ]; then echo "__REBOOT__=yes"
# Only when the newest installed kernel sorts after the running one (a custom or newer running
# kernel is not a pending reboot).
elif [ -n "$latest" ] && [ "$latest" != "$running" ] \
  && [ "$(printf '%s\n%s\n' "$running" "$latest" | $SORT | tail -n 1)" = "$latest" ]; then
  echo "__REBOOT__=yes"
elif command -v needs-restarting >/dev/null 2>&1; then
  if needs-restarting -r >/dev/null 2>&1; then echo "__REBOOT__=no"; else echo "__REBOOT__=yes"; fi
else echo "__REBOOT__=no"; fi
"""

CHECK_SCRIPT = (
    _PRELUDE
    + r"""
rc=0
case "$PM" in
  apt)
    apt-get $APT_OPTS update -q >&2 || rc=$?
    echo "__BEGIN__"
    apt list --upgradable 2>/dev/null | grep -v '^Listing'
    echo "__END__" ;;
  dnf|yum)
    REFRESH=""; [ "$PM" = dnf ] && REFRESH="--refresh"  # yum has no --refresh
    echo "__BEGIN__"
    $PM -q check-update $REFRESH 2>/dev/null; r=$?
    echo "__END__"
    # check-update exits 100 when updates exist, 1 on error.
    [ "$r" = 1 ] && rc=1 ;;
  apk)
    apk update -q >&2 || rc=$?
    echo "__BEGIN__"
    apk list -u 2>/dev/null
    echo "__END__" ;;
  pacman)
    # Never `pacman -Sy` the real database without upgrading (a partial upgrade); sync a copy
    # instead, which is what checkupdates does.
    db=$(mktemp -d)
    ln -s /var/lib/pacman/local "$db/local"
    pacman -Sy --dbpath "$db" --logfile /dev/null >&2 || rc=$?
    echo "__BEGIN__"
    pacman -Qu --dbpath "$db" 2>/dev/null
    echo "__END__"
    rm -rf "$db" ;;
  *)
    echo "No supported package manager found" >&2
    exit 3 ;;
esac
"""
    + REBOOT_SCRIPT
    + "exit $rc\n"
)

UPGRADE_SCRIPT = (
    _PRELUDE
    + r"""
set -e
case "$PM" in
  apt)
    apt-get $APT_OPTS update -q
    # dist-upgrade (a.k.a. full-upgrade) is what Proxmox and Raspberry Pi OS recommend.
    apt-get $APT_OPTS -y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold dist-upgrade ;;
  dnf) dnf -y upgrade --refresh ;;
  yum) yum -y upgrade ;;
  apk) apk update && apk upgrade ;;
  pacman) pacman -Syu --noconfirm ;;
  *) echo "No supported package manager found" >&2; exit 3 ;;
esac
"""
    + REBOOT_SCRIPT
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
    pending = ""
    for line in lines:
        if line.startswith("Obsoleting"):
            break  # what follows repeats packages already listed above, plus the ones they replace
        if line.startswith(("Last metadata", "Security:")):
            continue
        if line.startswith(" "):
            # dnf wraps long package names: the version and repo continue on the next line.
            if not pending:
                continue
            line, pending = f"{pending} {line}", ""
        parts = line.split()
        if len(parts) == 1:
            pending = parts[0]
            continue
        pending = ""
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
