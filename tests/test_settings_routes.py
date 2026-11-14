import pytest
import yaml
from fastapi.testclient import TestClient

from fleetdeck.app import create_app
from fleetdeck.config import load_config
from fleetdeck.security import hash_password

SSH = {"name": "NAS", "type": "ssh", "ssh_host": "192.168.1.30", "ssh_user": "root"}
API = {"name": "Portainer", "type": "docker-api", "url": "https://nas.lan:9443", "token": "secret-token-1"}


@pytest.fixture
def app_client(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "# my notes\nstate_file: state.db\nauth:\n  username: me\n  password_hash: "
        + hash_password("pw")
        + "\nmax_parallel: 2\n"
        "services:\n  sources:\n    - name: Old\n      type: docker-api\n      url: https://old.lan:2376\n"
        "      token: ${OLD_TOKEN}\n"
    )
    monkey_env = {"OLD_TOKEN": "x"}
    import os

    os.environ.update(monkey_env)
    app = create_app(load_config(str(path)), config_file=str(path))
    with TestClient(app) as c:
        c.auth = ("me", "pw")
        yield c, path
    os.environ.pop("OLD_TOKEN", None)


def body(rev, sources, **kw):
    return {"revision": rev, "sources": sources, **kw}


def test_get_masks_secrets(app_client):
    c, _ = app_client
    data = c.get("/api/settings/services").json()
    assert data["sources"][0]["token_set"] is True
    assert "token" not in data["sources"][0] and "x" not in str(data["sources"][0].get("url"))


def test_blank_secret_keeps_reference_and_other_keys(app_client):
    c, path = app_client
    cur = c.get("/api/settings/services").json()
    src = {**cur["sources"][0], "token": None, "previous_name": "Old", "name": "Old"}
    r = c.put("/api/settings/services", json=body(cur["revision"], [src], show_stopped=True))
    assert r.status_code == 200, r.text
    saved = yaml.safe_load(path.read_text())
    assert saved["services"]["sources"][0]["token"] == "${OLD_TOKEN}"
    assert saved["services"]["show_stopped"] is True
    assert saved["max_parallel"] == 2 and saved["auth"]["username"] == "me"


def test_changing_url_drops_old_token(app_client):
    c, path = app_client
    cur = c.get("/api/settings/services").json()
    src = {**cur["sources"][0], "previous_name": "Old", "url": "https://other.lan:2376"}
    r = c.put("/api/settings/services", json=body(cur["revision"], [src]))
    assert r.status_code == 200, r.text
    # The saved token is never sent to a new address.
    assert "token" not in yaml.safe_load(path.read_text())["services"]["sources"][0]


def test_add_sources_live(app_client):
    c, path = app_client
    cur = c.get("/api/settings/services").json()
    r = c.put("/api/settings/services", json=body(cur["revision"], [SSH, API]))
    assert r.status_code == 200, r.text
    saved = yaml.safe_load(path.read_text())["services"]["sources"]
    assert saved[0]["ssh"]["host"] == "192.168.1.30" and saved[0]["ssh"]["key_file"].endswith("id_ed25519")
    assert saved[1]["token"] == "${SECRET:services.Portainer.token}"
    assert b"secret-token-1" not in (path.parent / "state.db").read_bytes()
    assert (path.parent / "keys" / "secrets.key").stat().st_mode & 0o077 == 0
    assert [s["name"] for s in c.get("/api/services").json()["sources"]] == ["NAS", "Portainer"]
    assert "secret-token-1" not in c.get("/api/settings/services").text


def test_stale_revision_and_duplicate_names(app_client):
    c, _ = app_client
    cur = c.get("/api/settings/services").json()
    assert c.put("/api/settings/services", json=body("nope", [])).status_code == 409
    assert c.put("/api/settings/services", json=body(cur["revision"], [SSH, SSH])).status_code == 400


def test_validation_error_does_not_echo_secret(app_client):
    c, _ = app_client
    cur = c.get("/api/settings/services").json()
    r = c.put("/api/settings/services", json=body(cur["revision"], [{**API, "ssh_port": 0}]))
    assert r.status_code == 422 and "secret-token-1" not in r.text


def test_test_asks_to_trust_host_key(app_client, monkeypatch):
    c, _ = app_client
    import fleetdeck.onboarding as ob

    class K:
        public_data = b"k"

        def get_fingerprint(self, _):
            return "SHA256:abc"

    async def fake(host, port):
        return K()

    monkeypatch.setattr(ob, "fetch_host_key", fake)
    r = c.post("/api/settings/services/test", json={"source": SSH}).json()
    assert r["needs_confirm"] and r["fingerprint"] == "SHA256:abc"


def test_failed_save_leaves_no_secret_behind(app_client):
    c, path = app_client
    cur = c.get("/api/settings/services").json()
    bad = {**API, "name": "bad/name"}
    r = c.put("/api/settings/services", json=body(cur["revision"], [bad]))
    assert r.status_code == 400
    from fleetdeck.secrets_store import SecretStore

    assert SecretStore(path.parent / "state.db", path.parent / "keys").get("services.bad_name.token") is None


def test_migrate_plaintext_secrets(tmp_path):
    import os

    path = tmp_path / "config.yaml"
    path.write_text(
        "state_file: state.db\nauth:\n  username: me\n  password_hash: " + hash_password("pw") + "\n"
        "proxmox:\n  - name: pve\n    api_url: https://10.0.0.2:8006\n    token_id: root@pam!x\n"
        "    token_secret: plain-pve-secret\n    tls_fingerprint: " + "ab" * 32 + "\n"
        "notify:\n  - type: ntfy\n    url: https://ntfy.sh/x\n    token: ${KEEP_ME}\n"
    )
    os.environ["KEEP_ME"] = "e"
    try:
        app = create_app(load_config(str(path)), config_file=str(path))
        with TestClient(app) as c:
            c.auth = ("me", "pw")
            assert c.get("/api/settings/secrets").json()["plaintext"] == ["proxmox.pve.token_secret"]
            assert c.post("/api/settings/secrets/migrate").json() == {"moved": ["proxmox.pve.token_secret"]}
            saved = yaml.safe_load(path.read_text())
            assert saved["proxmox"][0]["token_secret"] == "${SECRET:proxmox.pve.token_secret}"
            assert saved["notify"][0]["token"] == "${KEEP_ME}"
            assert "plain-pve-secret" not in path.read_text()
            assert load_config(str(path)).proxmox[0].token_secret == "plain-pve-secret"
            assert c.get("/api/settings/secrets").json()["plaintext"] == []
    finally:
        os.environ.pop("KEEP_ME", None)


def test_file_reference(tmp_path):
    from fleetdeck.config import validate_raw

    f = tmp_path / "tok"
    f.write_text("from-file\n")
    cfg = validate_raw(
        {
            "dockhand": {
                "url": "https://d.lan",
                "token": f"${{FILE:{f}}}",
                "environments": [{"target_id": "t", "environment_id": 1}],
            }
        }
    )
    assert cfg.dockhand.token == "from-file"
