import pytest
from pydantic import ValidationError

from fleetdeck.config import AppConfig, AuthConfig, IncusConfig, load_config, validate_raw


def test_env_vars_expand_into_values_not_yaml(tmp_path, monkeypatch):
    # A secret with YAML-significant characters must survive intact.
    monkeypatch.setenv("FLEETDECK_PASSWORD", "abc #1: {x}")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("auth:\n  password: ${FLEETDECK_PASSWORD}\nhosts: []  # costs $5\n")
    cfg = load_config(str(cfg_file))
    assert cfg.auth.password == "abc #1: {x}"


def test_missing_env_var_is_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("NOPE_NOT_SET", raising=False)
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("auth:\n  password: ${NOPE_NOT_SET}\n")
    with pytest.raises(ValueError, match="NOPE_NOT_SET"):
        load_config(str(cfg_file))


def test_missing_config_file_falls_back_to_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("FLEETDECK_DEMO", "1")
    cfg = load_config(str(tmp_path / "absent.yaml"))
    assert cfg.demo and not cfg.proxmox


def test_validation():
    with pytest.raises(ValidationError):
        AuthConfig(password="")
    with pytest.raises(ValidationError):
        AppConfig(max_parallel=0)
    with pytest.raises(ValidationError, match="url or socket"):
        IncusConfig(name="i")
    with pytest.raises(ValidationError, match="unique"):
        AppConfig(incus=[IncusConfig(name="lab", socket="/s"), IncusConfig(name="lab", socket="/t")])


def test_warns_when_ssh_host_keys_are_not_checked(tmp_path, caplog):
    f = tmp_path / "c.yaml"
    f.write_text(
        "auth: {password: x}\n"
        "hosts:\n"
        "  - {name: pi, host: pi.lan}\n"
        "  - {name: nas, host: nas.lan, known_hosts: /config/known_hosts}\n"
        "  - {name: old, host: old.lan, insecure_skip_host_key_check: true}\n"
    )
    with caplog.at_level("WARNING", logger="fleetdeck.config"):
        load_config(str(f))
    assert "No known_hosts for pi.lan" in caplog.text and "nas.lan" not in caplog.text
    assert "off for old.lan" in caplog.text


def test_top_level_known_hosts_is_inherited():
    from fleetdeck.config import HostConfig, ProxmoxConfig, SSHConfig

    cfg = AppConfig(
        known_hosts="/config/known_hosts",
        hosts=[
            HostConfig(name="pi", host="pi.lan"),
            HostConfig(name="vps", host="vps", known_hosts="/config/vps_hosts"),
            HostConfig(name="old", host="old", insecure_skip_host_key_check=True),
        ],
        proxmox=[
            ProxmoxConfig(
                name="pve", api_url="https://pve:8006", token_id="t", token_secret="s",
                ssh=SSHConfig(host="pve"), node_ssh={"pve2": SSHConfig(host="pve2")},
            )
        ],
    )  # fmt: skip
    assert [h.known_hosts for h in cfg.hosts] == ["/config/known_hosts", "/config/vps_hosts", None]
    assert cfg.proxmox[0].ssh.known_hosts == cfg.proxmox[0].node_ssh["pve2"].known_hosts == "/config/known_hosts"


def test_tls_fingerprint_and_warning(tmp_path, caplog):
    from fleetdeck.config import ProxmoxConfig

    fp = "AB:" * 31 + "CD"
    p = ProxmoxConfig(name="pve", api_url="https://pve:8006", token_id="t", token_secret="s", tls_fingerprint=fp)
    assert p.tls_fingerprint == "ab" * 31 + "cd"
    with pytest.raises(ValidationError, match="SHA-256"):
        ProxmoxConfig(name="pve", api_url="https://pve:8006", token_id="t", token_secret="s", tls_fingerprint="AB:CD")

    f = tmp_path / "c.yaml"
    f.write_text(
        "auth: {password: x}\n"
        "proxmox:\n"
        "  - {name: insecure, api_url: 'https://a:8006', token_id: t, token_secret: s, verify_ssl: false}\n"
        "  - {name: pinned, api_url: 'https://b:8006', token_id: t, token_secret: s, verify_ssl: false,\n"
        f"     tls_fingerprint: '{fp}'}}\n"
    )
    with caplog.at_level("WARNING", logger="fleetdeck.config"):
        load_config(str(f))
    assert "checking is off for insecure" in caplog.text and "pinned" not in caplog.text


def test_example_config_is_valid(monkeypatch):
    from pathlib import Path

    monkeypatch.setenv("FLEETDECK_PASSWORD", "x")
    monkeypatch.setenv("PVE_TOKEN_SECRET", "y")
    cfg = load_config(str(Path(__file__).parent.parent / "config.example.yaml"))
    assert [h.name for h in cfg.hosts] == ["pi4", "vps"]


def test_relative_state_file_is_next_to_config(tmp_path, monkeypatch):
    monkeypatch.delenv("FLEETDECK_STATE", raising=False)
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("state_file: data/state.db\n")
    assert load_config(str(cfg_file)).state_file == str(tmp_path / "data" / "state.db")


def test_unreadable_files_are_reported(tmp_path, monkeypatch, caplog):
    import os

    import fleetdeck.config
    from fleetdeck.config import SSHConfig
    from fleetdeck.executors import SSHExecutor

    key = tmp_path / "id_ed25519"
    key.write_text("k")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(f"hosts:\n  - name: pi\n    host: 10.0.0.2\n    key_file: {key}\n")
    real_access = os.access
    monkeypatch.setattr(fleetdeck.config.os, "access", lambda p, m: str(p) != str(key) and real_access(p, m))
    load_config(str(cfg_file))
    assert f"can't read {key}" in caplog.text and "chown -R 10001:10001 config" in caplog.text
    with pytest.raises(RuntimeError, match="can't read"):
        SSHExecutor(SSHConfig(host="10.0.0.2", key_file=str(key), known_hosts="/kh"))._connect_kwargs()

    monkeypatch.setattr(fleetdeck.config.os, "access", lambda p, m: False)
    with pytest.raises(ValueError, match="can't read"):
        load_config(str(cfg_file))


def test_old_homelab_variable_name_still_expands(monkeypatch):
    monkeypatch.delenv("FLEETDECK_PASSWORD", raising=False)
    monkeypatch.setenv("FLEETDECK_PASSWORD", "x")
    cfg = validate_raw({"auth": {"password": "${FLEETDECK_PASSWORD}"}})
    assert cfg.auth.password == "x"


def test_community_scripts_update_is_on_by_default():
    from fleetdeck.config import AppConfig

    assert AppConfig().community_scripts_update is True


def test_update_commands_take_vmids_and_must_be_one_line():
    from fleetdeck.config import HostConfig, ProxmoxConfig

    pve = ProxmoxConfig(name="pve", api_url="https://x", token_id="a", token_secret="b", update_commands={105: " x "})
    assert pve.update_commands == {"105": "x"}
    assert HostConfig(name="nas", host="nas", update_command="omv-upgrade").update_command == "omv-upgrade"
    for bad in ("", "a\nb", "x" * 1001):
        with pytest.raises(ValidationError):
            HostConfig(name="nas", host="nas", update_command=bad)
    with pytest.raises(ValidationError):
        IncusConfig(name="i", socket="/s", update_commands={"web": "a\rb"})
