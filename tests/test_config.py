import pytest
from pydantic import ValidationError

from homelab.config import AppConfig, AuthConfig, IncusConfig, load_config


def test_env_vars_expand_into_values_not_yaml(tmp_path, monkeypatch):
    # A secret with YAML-significant characters must survive intact.
    monkeypatch.setenv("HOMELAB_PASSWORD", "abc #1: {x}")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("auth:\n  password: ${HOMELAB_PASSWORD}\nhosts: []  # costs $5\n")
    cfg = load_config(str(cfg_file))
    assert cfg.auth.password == "abc #1: {x}"


def test_missing_env_var_is_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("NOPE_NOT_SET", raising=False)
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("auth:\n  password: ${NOPE_NOT_SET}\n")
    with pytest.raises(ValueError, match="NOPE_NOT_SET"):
        load_config(str(cfg_file))


def test_missing_config_file_falls_back_to_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("HOMELAB_DEMO", "1")
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
    )
    with caplog.at_level("WARNING", logger="homelab.config"):
        load_config(str(f))
    assert "pi.lan" in caplog.text and "nas.lan" not in caplog.text


def test_example_config_is_valid(monkeypatch):
    from pathlib import Path

    monkeypatch.setenv("HOMELAB_PASSWORD", "x")
    monkeypatch.setenv("PVE_TOKEN_SECRET", "y")
    cfg = load_config(str(Path(__file__).parent.parent / "config.example.yaml"))
    assert [h.name for h in cfg.hosts] == ["pi4", "vps"]


def test_unreadable_files_are_reported(tmp_path, monkeypatch, caplog):
    import os

    import homelab.config
    from homelab.config import SSHConfig
    from homelab.executors import SSHExecutor

    key = tmp_path / "id_ed25519"
    key.write_text("k")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(f"hosts:\n  - name: pi\n    host: 10.0.0.2\n    key_file: {key}\n")
    real_access = os.access
    monkeypatch.setattr(homelab.config.os, "access", lambda p, m: str(p) != str(key) and real_access(p, m))
    load_config(str(cfg_file))
    assert f"can't read {key}" in caplog.text and "chown -R 10001:10001 config" in caplog.text
    with pytest.raises(RuntimeError, match="can't read"):
        SSHExecutor(SSHConfig(host="10.0.0.2", key_file=str(key)))._connect_kwargs()

    monkeypatch.setattr(homelab.config.os, "access", lambda p, m: False)
    with pytest.raises(ValueError, match="can't read"):
        load_config(str(cfg_file))
