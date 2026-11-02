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
    __PHASED__=package-one,package-two  (apt packages deferred for this machine; optional)
    __PHASED_BLOCKED__=package-three   (apt packages waiting on those phased packages; optional)
    __REBOOT__=yes|no|unknown
    __SELF__=yes            (only on the machine whose Docker/Podman runs this dashboard)
    __CSAPP__=plex          (only in a container installed by the community-scripts helper scripts)

Refresh logs go to stderr so they never pollute the package list.
"""

from __future__ import annotations

import re
import shlex
from datetime import UTC, datetime
from pathlib import Path

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

# Containers made by the Proxmox VE Helper-Scripts (community-scripts) get a /usr/bin/update that
# downloads and runs the app's own script (ct/<app>.sh). That script knows the app: it moves its
# apt repository when the vendor changes it, reinstalls or rebuilds it, and restarts it. A plain OS
# upgrade knows none of that, so after one the app may need its own update to work again.
COMMUNITY_SCRIPT_DETECT = r"""
if [ -f /usr/bin/update ] && grep -q 'community-scripts/' /usr/bin/update 2>/dev/null; then
  # Newer layout: a generated script that names the app in SCRIPT_SLUG.
  app=$(sed -n 's/^export SCRIPT_SLUG="\([A-Za-z0-9._-]*\)".*/\1/p' /usr/bin/update | head -n 1)
  [ -n "$app" ] || app=$(grep -o 'ct/[A-Za-z0-9._-]*[.]sh' /usr/bin/update | head -n 1 | sed 's#^ct/##; s#[.]sh$##')
  # Other layouts of the file: take the name of whatever .sh address it points at.
  [ -n "$app" ] || app=$(grep -o 'https://[^)"'"'"' ]*[.]sh' /usr/bin/update | head -n 1 | sed 's#.*/##; s#[.]sh$##')
  echo "__CSAPP__=${app:-unknown}"
fi
"""

# Runs the container's own `update` the way you would by hand. The helper scripts call `clear` and
# draw spinners, which fail without a terminal ("Operation not permitted" from `clear`), so it runs in
# a pseudo-terminal with TERM set. PHS_SILENT=1 skips its menus and prompts. Without `script`
# (util-linux) it runs `update` directly. The helper script fetches itself from GitHub, as it does
# when you type `update`.
COMMUNITY_UPDATE_SCRIPT = r"""
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH
export DEBIAN_FRONTEND=noninteractive PHS_SILENT=1 TERM=xterm-256color
if ! command -v update >/dev/null 2>&1; then echo "update is not installed" >&2; exit 3; fi
if command -v script >/dev/null 2>&1; then
  exec script -qec update /dev/null </dev/null
fi
exec update </dev/null
"""

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Za-z0-9]")


def clean_line(line: str) -> str:
    """A helper-script log line without colours, cursor moves or spinner frames (only what is
    left after the last carriage return is what a terminal would show)."""
    return _ANSI.sub("", line).rsplit("\r", 1)[-1].strip()


_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_SAFE_PATH = re.compile(r"^/[A-Za-z0-9/._-]+$")
# Where Docker and Podman keep a container's files (root and rootless snap/Docker defaults).
_CONTAINER_DIRS = (
    "/var/lib/docker/containers/{}",
    "/var/snap/docker/common/var-lib-docker/containers/{}",
    "/var/lib/containers/storage/overlay-containers/{}",
)


def self_script(mountinfo: str) -> str:
    """Shell that prints ``__SELF__=yes`` on the machine running this dashboard's container.

    Docker and Podman bind-mount the container's /etc/hostname from a folder named after the
    container's ID, so that ID is in our own mountinfo. Only the machine whose container engine runs
    us has that folder. Empty when we don't run in such a container (or the ID can't be found).
    """
    dirs: list[str] = []
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[4] != "/etc/hostname":
            continue
        root = fields[3]
        m = _CONTAINER_ID.search(root)
        if not m:
            continue
        cid = m[0]
        # The source path as the engine's host sees it, when the engine's folder is on its root filesystem.
        own = root[: m.end()]
        if _SAFE_PATH.match(own):
            dirs.append(own)
        dirs += [d.format(cid) for d in _CONTAINER_DIRS]
    if not dirs:
        return ""
    paths = " ".join(dict.fromkeys(dirs))
    return f'for d in {paths}; do if [ -d "$d" ]; then echo __SELF__=yes; break; fi; done\n'


def _own_mountinfo() -> str:
    try:
        return Path("/proc/self/mountinfo").read_text()
    except OSError:
        return ""


SELF_SCRIPT = self_script(_own_mountinfo())

DOCKER_DETECT_SCRIPT = (
    "if command -v docker >/dev/null 2>&1; then echo __DOCKER_INSTALLED__=yes; else echo __DOCKER_INSTALLED__=no; fi\n"
)

CHECK_SCRIPT = (
    _PRELUDE
    + r"""
rc=0
case "$PM" in
  apt)
    apt-get $APT_OPTS update -q >&2 || rc=$?
    echo "__BEGIN__"
    apt list --upgradable 2>/dev/null | grep -v '^Listing'
    echo "__END__"
    # Read apt's old and new output formats. A package in "Not upgrading" can still be blocked
    # by a related phased package (systemd commonly upgrades as a matched set), so compare with a
    # second read-only simulation that includes phased updates. The only changed input is phasing.
    apt_section() {
      awk -v old="$1" -v new="$2" '
        $0 == old || $0 == new { collect=1; next }
        collect && /^[[:space:]]/ {
          for (i=1; i<=NF; i++) {
            name=$i
            sub(/:[^:[:space:]]+$/, "", name)
            if (name ~ /^[a-z0-9][a-z0-9+.-]*$/) print name
          }
          next
        }
        collect { exit }
      '
    }
    simulation=$(apt-get $APT_OPTS -s -q upgrade 2>/dev/null || true)
    phased=$(printf '%s\n' "$simulation" | apt_section \
      "The following upgrades have been deferred due to phasing:" "Not upgrading yet due to phasing:" | sort -u)
    not_upgrading=$(printf '%s\n' "$simulation" | apt_section \
      "The following packages have been kept back:" "Not upgrading:" | sort -u)
    if [ -n "$phased" ]; then
      phased_csv=$(printf '%s\n' "$phased" | tr '\n' ',')
      echo "__PHASED__=${phased_csv%,}"
    fi
    if [ -n "$phased" ] && [ -n "$not_upgrading" ]; then
      forced=$(apt-get $APT_OPTS -s -q -o APT::Get::Always-Include-Phased-Updates=true upgrade 2>/dev/null || true)
      forced_upgrading=$(printf '%s\n' "$forced" | apt_section \
        "The following packages will be upgraded:" "Upgrading:" | sort -u)
      phased_blocked=""
      for package in $not_upgrading; do
        if printf '%s\n' "$forced_upgrading" | grep -F -x -q "$package"; then
          phased_blocked="${phased_blocked}${phased_blocked:+,}${package}"
        fi
      done
      [ -n "$phased_blocked" ] && echo "__PHASED_BLOCKED__=$phased_blocked"
    fi ;;
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
    + COMMUNITY_SCRIPT_DETECT
    + SELF_SCRIPT
    + "exit $rc\n"
)

UPGRADE_SCRIPT = (
    _PRELUDE
    + r"""
set -e
case "$PM" in
  apt)
    apt-get $APT_OPTS update -q
    # dist-upgrade (a.k.a. full-upgrade) is what Proxmox and Raspberry Pi OS recommend. Elsewhere it may
    # remove packages to resolve a conflict, unattended; there, new dependencies are installed but nothing
    # is removed, and an upgrade that needs a removal stays in the list for you to do by hand.
    if command -v pveversion >/dev/null 2>&1 || command -v proxmox-backup-manager >/dev/null 2>&1 \
      || command -v pmgversion >/dev/null 2>&1 || [ -e /etc/rpi-issue ]; then UPG=dist-upgrade
    else UPG="upgrade --with-new-pkgs"; fi
    apt-get $APT_OPTS -y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold $UPG ;;
  dnf) dnf -y upgrade --refresh ;;
  yum) yum -y upgrade ;;
  apk) apk update && apk upgrade ;;
  pacman) pacman -Syu --noconfirm ;;
  *) echo "No supported package manager found" >&2; exit 3 ;;
esac
"""
    + REBOOT_SCRIPT
    + COMMUNITY_SCRIPT_DETECT
)


# Only the packages whose new version comes from a security repository (the same rule parse_apt uses to
# flag them), so a scheduled run never pulls in feature updates. Nothing else is installed on its own
# account except the dependencies those packages need.
SECURITY_UPGRADE_SCRIPT = (
    _PRELUDE
    + r"""
set -e
case "$PM" in
  apt)
    apt-get $APT_OPTS update -q
    pkgs=$(apt list --upgradable 2>/dev/null | grep -E '^[^/ ]+/[^ ]*-security([, ]|$)' | cut -d/ -f1 | sort -u | tr '
' ' ') || true
    if [ -z "$pkgs" ]; then echo "== No security updates to install"
    else
      echo "== Installing security updates: $pkgs"
      apt-get $APT_OPTS -y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold \
        install --only-upgrade $pkgs
    fi ;;
  *) echo "Security-only updates are only supported with apt" >&2; exit 3 ;;
esac
"""
    + REBOOT_SCRIPT
)


# How a custom update script is told apart (by the demo).
CUSTOM_MARK = "</dev/null\nrc=$?\n"


def custom_update_script(command: str) -> str:
    """A machine's own update command (e.g. omv-upgrade, set in the config or on the dashboard) instead of the
    package-manager upgrade, then the same reboot check. Its exit code is the job's: no fallback to UPGRADE_SCRIPT.
    stdin is /dev/null, so a command that asks a question fails instead of hanging."""
    return _PRELUDE + f"sh -c {shlex.quote(command)} " + CUSTOM_MARK + REBOOT_SCRIPT + "exit $rc\n"


# Identifies one boot of the target, so a reboot can be confirmed: the kernel's boot_id changes
# when a host or VM reboots, and PID 1's start time (field 22 of /proc/1/stat, counted after the
# command name, which may contain spaces) changes when a container restarts on a running kernel.
BOOT_ID_SCRIPT = r"""
b=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)
s=$(sed 's/.*) //' /proc/1/stat 2>/dev/null | cut -d' ' -f20)
echo "__BOOT__=$b-$s"
"""

# Reboots in the background after a short delay, so the command returns (and the SSH session or
# exec call closes cleanly) before the machine goes down.
REBOOT_NOW_SCRIPT = r"""
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then cmd="systemctl reboot"
else cmd="reboot"; fi
if command -v setsid >/dev/null 2>&1; then detach=setsid; else detach=""; fi
$detach nohup sh -c "sleep 2; $cmd" >/dev/null 2>&1 </dev/null &
echo "== Reboot scheduled ($cmd)"
"""


def parse_boot_id(stdout: str) -> str | None:
    meta, _ = _markers(stdout)
    boot = meta.get("BOOT", "").strip("-")
    return boot or None


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


_APT = re.compile(
    r"^(?P<name>[^/\s]+)/(?P<suites>\S+)\s+(?P<new>\S+)\s+\S+(?:\s+\[upgradable from: (?P<old>[^\]]+)\])?"
)
_APK = re.compile(r"^(?P<pkg>\S+)\s.*?(?:\[upgradable from: (?P<old>[^\]]+)\])?$")
_APK_VER = re.compile(r"^(?P<name>.+?)-(?P<ver>\d[^-]*-r\d+)$")


def parse_apt(lines: list[str]) -> list[Package]:
    out = []
    for line in lines:
        m = _APT.match(line)
        if m:
            # The suites the new version comes from, e.g. "bookworm-security" or "jammy-updates,jammy-security".
            security = any(s.endswith("-security") for s in m["suites"].split(","))
            out.append(Package(name=m["name"], available=m["new"], current=m["old"], security=security))
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
    phased_direct = {name for name in meta.get("PHASED", "").split(",") if name}
    phased_dependency = {name for name in meta.get("PHASED_BLOCKED", "").split(",") if name}
    phased = phased_direct | phased_dependency
    if phased:
        packages = [
            p.model_copy(update={"phased": p.name in phased, "phased_dependency": p.name in phased_dependency})
            for p in packages
        ]
    if parser is None and error is None:
        error = f"unsupported package manager: {pm or 'unknown'}"
    return CheckResult(
        package_manager=pm,
        os=meta.get("OS"),
        packages=packages,
        reboot_required=_reboot(meta),
        checked_at=now,
        error=error,
        runs_dashboard=meta.get("SELF") == "yes",
        community_script=parse_community_script(result.stdout),
        docker_installed={"yes": True, "no": False}.get(meta.get("DOCKER_INSTALLED")),
    )


def parse_reboot(stdout: str) -> bool | None:
    meta, _ = _markers(stdout)
    return _reboot(meta)


def parse_docker_installed(stdout: str) -> bool | None:
    meta, _ = _markers(stdout)
    return {"yes": True, "no": False}.get(meta.get("DOCKER_INSTALLED"))


def parse_community_script(stdout: str) -> str | None:
    """The helper-script app (e.g. "plex") the check or upgrade found, if any."""
    meta, _ = _markers(stdout)
    return meta.get("CSAPP") or None
