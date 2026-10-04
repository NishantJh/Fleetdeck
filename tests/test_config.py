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
