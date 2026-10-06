from homelab.models import ExecResult
from homelab.updates import parse_apk, parse_apt, parse_check, parse_dnf, parse_pacman


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
