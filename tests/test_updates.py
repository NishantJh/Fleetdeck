import subprocess

from fleetdeck.models import ExecResult
from fleetdeck.updates import (
    CHECK_SCRIPT,
    COMMUNITY_SCRIPT_DETECT,
    COMMUNITY_UPDATE_SCRIPT,
    clean_line,
    custom_update_script,
    parse_apk,
    parse_apt,
    parse_check,
    parse_dnf,
    parse_pacman,
)


def test_parse_apt():
    pkgs = parse_apt(
        [
            "curl/stable-security 7.88.1-10+deb12u8 amd64 [upgradable from: 7.88.1-10+deb12u7]",
            "openssh-server/stable 1:9.2p1-2+deb12u4 arm64 [upgradable from: 1:9.2p1-2+deb12u3]",
        ]
    )
    assert [(p.name, p.current, p.available) for p in pkgs] == [
        ("curl", "7.88.1-10+deb12u7", "7.88.1-10+deb12u8"),
        ("openssh-server", "1:9.2p1-2+deb12u3", "1:9.2p1-2+deb12u4"),
    ]
    assert [p.security for p in pkgs] == [True, False]


def test_parse_apt_security_suites():
    pkgs = parse_apt(
        [
            "libssl3t64/noble-updates,noble-security 3.0.13-0ubuntu3.5 amd64 [upgradable from: 3.0.13-0ubuntu3.4]",
            "tzdata/noble-updates 2024b-0ubuntu0.24.04 all [upgradable from: 2024a-0ubuntu0.24.04]",
            "openssl/bookworm-security 3.0.15-1~deb12u1 arm64 [upgradable from: 3.0.14-1~deb12u2]",
            "fake/my-securityish-repo 1.0 all [upgradable from: 0.9]",
        ]
    )
    assert [p.security for p in pkgs] == [True, False, True, False]


def test_parse_dnf():
    pkgs = parse_dnf(
        [
            "kernel.x86_64                6.10.6-200.fc40          updates",
            "python3-libs.x86_64          3.12.5-1.fc40            updates",
            "Obsoleting Packages",
            "    grub2-tools.x86_64        1:2.06-123.fc40          updates",
        ]
    )
    assert [(p.name, p.available) for p in pkgs] == [("kernel", "6.10.6-200.fc40"), ("python3-libs", "3.12.5-1.fc40")]


def test_parse_dnf_wrapped_and_obsoleting():
    pkgs = parse_dnf(
        [
            "a-very-long-package-name-that-wraps.noarch",
            "                             1.2.3-1.fc40             updates",
            "bash.x86_64                  5.2.26-4.fc40            updates",
            "Obsoleting Packages",
            "grub2-tools.x86_64           1:2.06-123.fc40          updates",
            "    grub2-tools.x86_64       1:2.06-100.fc40          @fedora",
        ]
    )
    assert [(p.name, p.available) for p in pkgs] == [
        ("a-very-long-package-name-that-wraps", "1.2.3-1.fc40"),
        ("bash", "5.2.26-4.fc40"),
    ]


def test_parse_apk():
    pkgs = parse_apk(["busybox-1.36.1-r31 x86_64 {busybox} (GPL-2.0-only) [upgradable from: busybox-1.36.1-r29]"])
    assert [(p.name, p.current, p.available) for p in pkgs] == [("busybox", "1.36.1-r29", "1.36.1-r31")]


def test_parse_pacman():
    pkgs = parse_pacman(["linux 6.10.5.arch1-1 -> 6.10.6.arch1-1"])
    assert [(p.name, p.current, p.available) for p in pkgs] == [("linux", "6.10.5.arch1-1", "6.10.6.arch1-1")]


def test_parse_check_full_output():
    out = (
        "__PM__=apt\n__OS__=Raspberry Pi OS\n__BEGIN__\n"
        "tzdata/stable-updates 2025a-0+deb12u1 all [upgradable from: 2024b-0+deb12u1]\n"
        "__END__\n__REBOOT__=yes\n"
    )
    res = parse_check(ExecResult(rc=0, stdout=out))
    assert res.package_manager == "apt"
    assert res.os == "Raspberry Pi OS"
    assert res.reboot_required is True
    assert [p.name for p in res.packages] == ["tzdata"]
    assert res.error is None


def test_parse_check_marks_only_packages_deferred_by_apt_phasing():
    out = (
        "__PM__=apt\n__OS__=Ubuntu 24.04 LTS\n__BEGIN__\n"
        "curl/noble-updates 2 amd64 [upgradable from: 1]\n"
        "tzdata/noble-updates 2025a all [upgradable from: 2024b]\n"
        "__END__\n__PHASED__=tzdata\n__REBOOT__=no\n"
    )
    res = parse_check(ExecResult(rc=0, stdout=out))
    assert [(p.name, p.phased) for p in res.packages] == [("curl", False), ("tzdata", True)]


def test_parse_check_marks_packages_blocked_by_a_phased_transaction():
    out = (
        "__PM__=apt\n__OS__=Ubuntu 24.04 LTS\n__BEGIN__\n"
        "systemd-sysv/noble-updates 2 amd64 [upgradable from: 1]\n"
        "libsystemd0/noble-updates 2 amd64 [upgradable from: 1]\n"
        "__END__\n__PHASED__=systemd-sysv\n__PHASED_BLOCKED__=libsystemd0\n__REBOOT__=no\n"
    )
    res = parse_check(ExecResult(rc=0, stdout=out))
    assert [(p.name, p.phased, p.phased_dependency) for p in res.packages] == [
        ("systemd-sysv", True, False),
        ("libsystemd0", True, True),
    ]


def test_check_script_detects_the_whole_package_group_waiting_on_phasing(tmp_path):
    direct = ["systemd-sysv", "systemd-timesyncd", "udev"]
    dependent = [
        "libpam-systemd",
        "libsystemd-shared",
        "libsystemd0",
        "libudev1",
        "systemd",
        "systemd-cryptsetup",
        "systemd-resolved",
    ]
    packages = direct + dependent
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    apt_lines = "\n".join(
        f"{name}/noble-updates 255.4-1ubuntu8.12 amd64 [upgradable from: 255.4-1ubuntu8.11]" for name in packages
    )
    (bin_dir / "apt").write_text(
        f"#!/bin/sh\nif [ \"$1\" = list ]; then\n  echo Listing...\n  printf '%s\\n' '{apt_lines}'\nfi\n"
    )
    (bin_dir / "apt-get").write_text(
        """#!/bin/sh
case " $* " in
  *" update "*) exit 0 ;;
  *"Always-Include-Phased-Updates=true"*)
    cat <<'EOF'
Upgrading:
  systemd-sysv systemd-timesyncd udev
  libpam-systemd libsystemd-shared libsystemd0 libudev1
  systemd systemd-cryptsetup systemd-resolved
Summary:
  Upgrading: 10, Installing: 0, Removing: 0, Not Upgrading: 0
EOF
    ;;
  *)
    cat <<'EOF'
Not upgrading yet due to phasing:
  systemd-sysv systemd-timesyncd udev

Not upgrading:
  libpam-systemd libsystemd-shared libsystemd0 libudev1
  systemd systemd-cryptsetup systemd-resolved

Summary:
  Upgrading: 0, Installing: 0, Removing: 0, Not Upgrading: 10
Notice: Some packages may have been kept back due to phasing.
EOF
    ;;
esac
"""
    )
    for path in bin_dir.iterdir():
        path.chmod(0o755)
    modules = tmp_path / "modules"
    modules.mkdir()
    script = CHECK_SCRIPT.replace(
        "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH",
        "",
    )
    result = subprocess.run(
        ["sh", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "MODULES_DIR": str(modules)},
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    parsed = parse_check(ExecResult(rc=result.returncode, stdout=result.stdout, stderr=result.stderr))
    assert len(parsed.packages) == 10
    assert {p.name for p in parsed.packages if p.phased and not p.phased_dependency} == set(direct)
    assert {p.name for p in parsed.packages if p.phased_dependency} == set(dependent)


def test_parse_check_error():
    res = parse_check(ExecResult(rc=100, stdout="__PM__=apt\n", stderr="E: Could not get lock\n"))
    assert res.error and "Could not get lock" in res.error


def test_parse_check_unknown_pm():
    res = parse_check(ExecResult(rc=0, stdout="__PM__=unknown\n"))
    assert res.error and "unsupported" in res.error


# What the helper scripts' install.func writes to /usr/bin/update (no #! line).
_CS_UPDATE = (
    "set -a; [ -f /etc/profile.d/90-http-proxy.sh ] && . /etc/profile.d/90-http-proxy.sh; set +a; "
    'bash -c "$(curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct/plex.sh)"\n'
)


def _sh(script: str, update_file, path_dir=None) -> subprocess.CompletedProcess:
    script = script.replace("/usr/bin/update", str(update_file))
    # Keep the fake curl and bash first on PATH (the real script puts the system's first).
    script = script.replace("export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH", "")
    env = {"PATH": f"{path_dir}:/usr/bin:/bin" if path_dir else "/usr/bin:/bin"}
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, env=env, timeout=30)


def test_detects_community_scripts_container(tmp_path):
    f = tmp_path / "update"
    f.write_text(_CS_UPDATE)
    r = _sh(COMMUNITY_SCRIPT_DETECT, f)
    assert r.stdout.strip() == "__CSAPP__=plex"
    meta = parse_check(ExecResult(rc=0, stdout="__PM__=apt\n__BEGIN__\n__END__\n" + r.stdout))
    assert meta.community_script == "plex"


def test_other_update_command_is_not_a_community_script(tmp_path):
    f = tmp_path / "update"
    f.write_text("#!/bin/sh\napt-get update && apt-get upgrade\n")
    assert _sh(COMMUNITY_SCRIPT_DETECT, f).stdout == ""
    assert _sh(COMMUNITY_SCRIPT_DETECT, tmp_path / "missing").stdout == ""
    assert parse_check(ExecResult(rc=0, stdout="__PM__=apt\n")).community_script is None


def test_other_update_file_layouts(tmp_path):
    f = tmp_path / "update"
    f.write_text(
        'bash -c "$(curl -fsSL https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/x/plex.sh)"\n'
    )
    assert _sh(COMMUNITY_SCRIPT_DETECT, f).stdout.strip() == "__CSAPP__=plex"
    f.write_text("# community-scripts/ProxmoxVE\nupdate_app\n")
    assert _sh(COMMUNITY_SCRIPT_DETECT, f).stdout.strip() == "__CSAPP__=unknown"


_CS_UPDATE_NEW = """#!/usr/bin/env bash
export SCRIPT_SLUG="plex"
export UPDATE_SCRIPT_NAME="plex"
export COMMUNITY_SCRIPTS_URL="https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main"
export COMMUNITY_SCRIPTS_CORE_URL="https://raw.githubusercontent.com/community-scripts/core/main"
_cs_helper="$(curl -fsSL "${COMMUNITY_SCRIPTS_CORE_URL}/misc/update.sh" 2>/dev/null)"
if [[ -n "$_cs_helper" ]]; then bash -c "$_cs_helper"; fi
"""


def test_newer_generated_update_file_is_detected(tmp_path):
    f = tmp_path / "update"
    f.write_text(_CS_UPDATE_NEW)
    assert _sh(COMMUNITY_SCRIPT_DETECT, f).stdout.strip() == "__CSAPP__=plex"


def _run_update(tmp_path, with_script: bool):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "calls"
    (bin_dir / "update").write_text(
        f'#!/bin/sh\necho "update TERM=$TERM PHS_SILENT=$PHS_SILENT" >> {log.as_posix()}\nexit 7\n'
    )
    if with_script:
        # Fake util-linux script: `script -qec CMD /dev/null` runs CMD.
        (bin_dir / "script").write_text(f'#!/bin/sh\necho "script $*" >> {log.as_posix()}\nexec sh -c "$2"\n')
    for p in bin_dir.iterdir():
        p.chmod(0o755)
    script = COMMUNITY_UPDATE_SCRIPT.replace(
        "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH", ""
    )
    r = subprocess.run(
        ["sh", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir.as_posix()}:/usr/bin:/bin"},
        timeout=30,
    )
    return r, (log.read_text().splitlines() if log.exists() else [])


def test_helper_update_runs_in_a_terminal(tmp_path):
    r, calls = _run_update(tmp_path, with_script=True)
    assert r.returncode == 7  # update's own exit code comes through
    assert calls[0] == "script -qec update /dev/null"
    assert calls[1] == "update TERM=xterm-256color PHS_SILENT=1"


def test_helper_update_without_script_command(tmp_path):
    r, calls = _run_update(tmp_path, with_script=False)
    assert r.returncode == 7
    assert calls == ["update TERM=xterm-256color PHS_SILENT=1"]


def test_clean_line_drops_colours_and_spinner_frames():
    assert clean_line("\x1b[33m⏳\x1b[m Updating\r\x1b[32m✔️\x1b[m Updated  ") == "✔️ Updated"
    assert clean_line("\x1b[?25l") == ""


def test_apt_upgrade_never_removes_packages_except_on_proxmox_and_raspberry_pi_os():
    from fleetdeck.updates import UPGRADE_SCRIPT

    assert "UPG=dist-upgrade" in UPGRADE_SCRIPT
    assert "pveversion" in UPGRADE_SCRIPT and "/etc/rpi-issue" in UPGRADE_SCRIPT
    assert 'UPG="upgrade --with-new-pkgs"' in UPGRADE_SCRIPT and "$UPG" in UPGRADE_SCRIPT


def test_custom_update_script_keeps_the_command_and_its_exit_code():
    # Quotes reach the command as typed; a question it asks gets end-of-input instead of hanging.
    script = custom_update_script("""echo "it's $((1+1))"; read x || echo no-input; exit 3""")
    p = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=30, stdin=subprocess.PIPE)
    assert p.returncode == 3
    assert "it's 2\nno-input\n" in p.stdout and "__REBOOT__=" in p.stdout
