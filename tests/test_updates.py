import subprocess

from fleetdeck.models import ExecResult
from fleetdeck.updates import (
    COMMUNITY_SCRIPT_DETECT,
    COMMUNITY_UPDATE_SCRIPT,
    clean_line,
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


def test_helper_update_runs_unattended_and_stops_when_download_fails(tmp_path):
    f = tmp_path / "update"
    f.write_text(_CS_UPDATE)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls"
    # Fake curl and bash that record how they were called; curl fails when FAIL exists.
    (bin_dir / "curl").write_text(f'#!/bin/sh\necho "curl $*" >> {log}\n[ ! -e {tmp_path}/FAIL ]\n')
    (bin_dir / "bash").write_text(f'#!/bin/sh\necho "bash PHS_SILENT=$PHS_SILENT" >> {log}\n')
    for p in bin_dir.iterdir():
        p.chmod(0o755)
    r = _sh(COMMUNITY_UPDATE_SCRIPT, f, bin_dir)
    assert r.returncode == 0, r.stderr
    calls = log.read_text().splitlines()
    assert (
        calls[0]
        == "curl -fsS -o /dev/null https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct/plex.sh"
    )
    assert "bash PHS_SILENT=1" in calls

    (tmp_path / "FAIL").touch()
    log.unlink()
    r = _sh(COMMUNITY_UPDATE_SCRIPT, f, bin_dir)
    assert r.returncode == 4 and "can't download" in r.stderr
    assert not any(c.startswith("bash") for c in log.read_text().splitlines())


def test_clean_line_drops_colours_and_spinner_frames():
    assert clean_line("\x1b[33m⏳\x1b[m Updating\r\x1b[32m✔️\x1b[m Updated  ") == "✔️ Updated"
    assert clean_line("\x1b[?25l") == ""
