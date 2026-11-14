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
    with TestClient(app, client=("192.168.1.5", 50000)) as c:
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


def test_schedule_save_applies_live_and_keeps_other_keys(app_client):
    c, path = app_client
    cur = c.get("/api/settings/schedule").json()
    assert cur["check_every"] == "" and cur["check_docker"] is True
    r = c.put("/api/settings/schedule", json={**cur, "check_every": "6h", "auto_security_updates": True})
    assert r.status_code == 200 and r.json()["restart_needed"] == []
    saved = yaml.safe_load(path.read_text())
    assert saved["schedule"]["check_every"] == "6h" and saved["schedule"]["auto_security_updates"] is True
    assert saved["max_parallel"] == 2
    sched = c.app.state.scheduler
    assert sched.interval == 21600 and sched.auto_security is True and sched._task is not None
    cur = c.get("/api/settings/schedule").json()
    assert cur["check_every"] == "6h"
    c.put("/api/settings/schedule", json={**cur, "check_every": ""})
    assert sched.interval is None and sched._task is None


def test_schedule_rejects_bad_interval(app_client):
    c, path = app_client
    cur = c.get("/api/settings/schedule").json()
    before = path.read_text()
    assert c.put("/api/settings/schedule", json={**cur, "check_every": "1m"}).status_code == 400
    assert c.put("/api/settings/schedule", json={**cur, "check_every": "soon"}).status_code == 400
    assert path.read_text() == before


def test_security_save_updates_scanner(app_client):
    c, path = app_client
    cur = c.get("/api/settings/security").json()
    r = c.put("/api/settings/security", json={**cur, "min_severity": "CRITICAL", "ignore_unfixed": False})
    assert r.status_code == 200 and r.json()["restart_needed"] == []
    assert yaml.safe_load(path.read_text())["security"]["min_severity"] == "CRITICAL"
    assert c.app.state.vulns.scanner.min_severity == "CRITICAL"
    cur = c.get("/api/settings/security").json()
    r = c.put("/api/settings/security", json={**cur, "scan_images": False})
    assert r.json()["restart_needed"] == ["Image scanning"]
    assert c.put("/api/settings/security", json={**cur, "min_severity": "bogus"}).status_code == 422


def test_updates_save_and_restart_note(app_client):
    c, path = app_client
    cur = c.get("/api/settings/updates").json()
    assert cur["max_parallel"] == 2
    r = c.put("/api/settings/updates", json={**cur, "snapshot_keep": 5, "community_scripts_update": False})
    assert r.status_code == 200 and r.json()["restart_needed"] == []
    assert c.app.state.jobs.snapshot_keep == 5 and c.app.state.jobs.community_scripts_update is False
    cur = c.get("/api/settings/updates").json()
    r = c.put("/api/settings/updates", json={**cur, "max_parallel": 6})
    assert r.json()["restart_needed"] == ["Parallel jobs"]
    assert c.put("/api/settings/updates", json={**cur, "snapshot_keep": 0}).status_code == 422


def test_section_stale_revision_conflicts(app_client):
    c, _ = app_client
    cur = c.get("/api/settings/updates").json()
    assert c.put("/api/settings/updates", json={**cur, "snapshot_keep": 4}).status_code == 200
    assert c.put("/api/settings/updates", json={**cur, "snapshot_keep": 5}).status_code == 409


@pytest.fixture
def notify_client(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "state_file: state.db\nauth:\n  username: me\n  password_hash: "
        + hash_password("pw")
        + "\nnotify:\n  - type: gotify\n    name: Phone\n    url: https://g.lan\n    token: gotify-token-1\n"
        "  - type: discord\n    url: https://discord.com/api/webhooks/1/secretpart\n"
    )
    app = create_app(load_config(str(path)), config_file=str(path))
    with TestClient(app) as c:
        c.auth = ("me", "pw")
        yield c, path, app


def test_notify_get_hides_secrets(notify_client):
    c, _, _ = notify_client
    r = c.get("/api/settings/notify")
    t = r.json()["targets"]
    assert t[0]["token_set"] is True and t[0]["url"] == "https://g.lan"
    assert t[1]["url"] == "" and t[1]["url_set"] is True
    assert "gotify-token-1" not in r.text and "secretpart" not in r.text


def test_notify_blank_keeps_and_new_goes_to_store(notify_client):
    c, path, app = notify_client
    cur = c.get("/api/settings/notify").json()
    keep = {**cur["targets"][0], "token": None, "previous_index": 0, "name": "Phone 2"}
    keep_hook = {**cur["targets"][1], "url": "", "previous_index": 1}
    new = {"type": "telegram", "name": "TG", "token": "123:abc", "chat_id": "42"}
    r = c.put("/api/settings/notify", json={"revision": cur["revision"], "targets": [keep, keep_hook, new]})
    assert r.status_code == 200, r.text
    text = path.read_text()
    saved = yaml.safe_load(text)["notify"]
    assert saved[0]["token"] == "gotify-token-1" and saved[0]["name"] == "Phone 2"
    assert saved[1]["url"].endswith("secretpart")
    assert saved[2]["token"] == "${SECRET:notify.TG.token}" and "123:abc" not in text
    # applied live
    assert [t.type for t in app.state.notifier.targets] == ["gotify", "discord", "telegram"]
    assert load_config(str(path)).notify[2].token == "123:abc"


def test_notify_secret_url_stored_encrypted(notify_client):
    c, path, _ = notify_client
    cur = c.get("/api/settings/notify").json()
    hook = {"type": "webhook", "name": "Hook", "url": "https://hooks.example/abc123"}
    r = c.put("/api/settings/notify", json={"revision": cur["revision"], "targets": [hook]})
    assert r.status_code == 200, r.text
    assert "abc123" not in path.read_text()
    assert yaml.safe_load(path.read_text())["notify"][0]["url"] == "${SECRET:notify.Hook.url}"


def test_notify_changed_address_drops_token_and_remove_all(notify_client):
    c, path, app = notify_client
    cur = c.get("/api/settings/notify").json()
    moved = {**cur["targets"][0], "token": None, "previous_index": 0, "url": "https://other.lan"}
    r = c.put("/api/settings/notify", json={"revision": cur["revision"], "targets": [moved]})
    assert r.status_code == 400 and "token" in r.json()["detail"]  # gotify needs its token again
    r = c.put("/api/settings/notify", json={"revision": cur["revision"], "targets": []})
    assert r.status_code == 200
    assert "notify" not in yaml.safe_load(path.read_text())
    assert app.state.notifier.targets == []


def test_notify_stale_revision_and_duplicate_names(notify_client):
    c, _, _ = notify_client
    cur = c.get("/api/settings/notify").json()
    a = {"type": "webhook", "name": "x", "url": "https://h.example/1"}
    assert c.put("/api/settings/notify", json={"revision": "old", "targets": [a]}).status_code == 409
    assert c.put("/api/settings/notify", json={"revision": cur["revision"], "targets": [a, a]}).status_code == 400


def test_notify_test_sends_without_saving(notify_client, monkeypatch):
    import httpx

    from fleetdeck.notify import Notifier

    c, path, _ = notify_client
    before = path.read_text()
    seen = []

    async def fake(self, t, title, body):
        seen.append((t.type, t.token))
        return httpx.Response(200)

    monkeypatch.setattr(Notifier, "_send_one", fake)
    cur = c.get("/api/settings/notify").json()
    t = {**cur["targets"][0], "token": None, "previous_index": 0}
    assert c.post("/api/settings/notify/test", json=t).json() == {"ok": True}
    assert seen == [("gotify", "gotify-token-1")]  # blank token: the saved one is used
    assert path.read_text() == before

    async def refused(self, t, title, body):
        return httpx.Response(401, text="bad gotify-token-1")

    monkeypatch.setattr(Notifier, "_send_one", refused)
    r = c.post("/api/settings/notify/test", json=t).json()
    assert r["ok"] is False and "401" in r["error"] and "gotify-token-1" not in r["error"]

    async def boom(self, t, title, body):
        raise httpx.ConnectError("https://api.telegram.org/bot123:abc/sendMessage")

    monkeypatch.setattr(Notifier, "_send_one", boom)
    r = c.post("/api/settings/notify/test", json={"type": "telegram", "token": "123:abc", "chat_id": "1"}).json()
    assert r["ok"] is False and "123:abc" not in r["error"]


def test_notify_removed_and_renamed_targets_delete_their_secrets(notify_client):
    from fleetdeck.secrets_store import SecretStore

    c, path, _ = notify_client
    store = SecretStore(path.parent / "state.db", path.parent / "keys")
    rev = c.get("/api/settings/notify").json()["revision"]
    a = {"type": "webhook", "name": "A", "url": "https://h.example/a"}
    b = {"type": "webhook", "name": "B", "url": "https://h.example/b"}
    assert c.put("/api/settings/notify", json={"revision": rev, "targets": [a, b]}).status_code == 200
    assert store.get("notify.A.url") and store.get("notify.B.url")
    rev = c.get("/api/settings/notify").json()["revision"]
    # remove A, keep B (blank url keeps its reference)
    keep = {"type": "webhook", "name": "B", "url": "", "previous_index": 1}
    assert c.put("/api/settings/notify", json={"revision": rev, "targets": [keep]}).status_code == 200
    assert store.get("notify.A.url") is None and store.get("notify.B.url")
    # rename B with a new address: the old secret goes, the new one stays
    rev = c.get("/api/settings/notify").json()["revision"]
    ren = {"type": "webhook", "name": "C", "url": "https://h.example/c", "previous_index": 0}
    assert c.put("/api/settings/notify", json={"revision": rev, "targets": [ren]}).status_code == 200
    assert store.get("notify.B.url") is None and store.get("notify.C.url")


def _guarded(c, section, **kw):
    cur = c.get(f"/api/settings/{section}").json()
    return {**cur, "password": "pw", **kw}


def test_console_requires_password_and_saves(app_client):
    c, path = app_client
    cur = _guarded(c, "console", enabled=True, password="nope")
    assert c.put("/api/settings/console", json=cur).status_code == 403
    assert "console" not in yaml.safe_load(path.read_text())
    r = c.put(
        "/api/settings/console", json={**cur, "password": "pw", "unlock_minutes": 5, "exclude": ["a", "b, c", "a"]}
    )
    assert r.status_code == 200 and r.json()["saved"] is True, r.text
    assert r.json()["restart_needed"] == ["Console"]
    saved = yaml.safe_load(path.read_text())["console"]
    assert saved["enabled"] is True and saved["unlock_minutes"] == 5 and saved["exclude"] == ["a", "b", "c"]
    assert yaml.safe_load(path.read_text())["max_parallel"] == 2
    assert c.get("/api/settings/console").json()["restart_pending"] is True


def test_console_lockout_needs_confirm(app_client):
    c, path = app_client
    cur = _guarded(c, "console", enabled=True, allow_from=["203.0.113.0/24"])
    r = c.put("/api/settings/console", json=cur)
    assert r.status_code == 200 and r.json()["saved"] is False and r.json()["warnings"]
    assert "console" not in yaml.safe_load(path.read_text())
    r = c.put("/api/settings/console", json={**cur, "confirm": True})
    assert r.json()["saved"] is True
    assert yaml.safe_load(path.read_text())["console"]["allow_from"] == ["203.0.113.0/24"]


def test_console_rejects_bad_network_and_stale_revision(app_client):
    c, path = app_client
    cur = _guarded(c, "console", allow_from=["not-a-network"])
    assert c.put("/api/settings/console", json=cur).status_code == 400
    cur = _guarded(c, "console")
    assert c.put("/api/settings/console", json={**cur, "keep_days": 30}).status_code == 200
    assert c.put("/api/settings/console", json={**cur, "keep_days": 31}).status_code == 409


def test_network_validates_and_saves(app_client):
    c, path = app_client
    cur = _guarded(c, "network")
    for bad in (
        {"public_url": "ftp://x"},
        {"allowed_hosts": ["http://x.lan"]},
        {"allowed_hosts": ["host:8080"]},
        {"trusted_proxies": ["proxy.lan"]},
        {"known_hosts": "a\nb"},
    ):
        assert c.put("/api/settings/network", json={**cur, **bad}).status_code == 400, bad
    r = c.put(
        "/api/settings/network",
        json={
            **cur,
            "public_url": "http://192.168.1.50:8080/",
            "allowed_hosts": ["testserver", "*.lan"],
            "trusted_proxies": ["172.18.0.0/16"],
        },
    )
    assert r.status_code == 200 and r.json()["saved"] is True, r.text
    saved = yaml.safe_load(path.read_text())
    assert saved["public_url"] == "http://192.168.1.50:8080"
    assert saved["allowed_hosts"] == ["testserver", "*.lan"] and saved["trusted_proxies"] == ["172.18.0.0/16"]
    assert set(r.json()["restart_needed"]) == {"Dashboard address", "Allowed host names", "Trusted proxies"}


def test_network_allowed_hosts_without_current_host_needs_confirm(app_client):
    c, path = app_client
    cur = _guarded(c, "network", allowed_hosts=["other.lan"])
    r = c.put("/api/settings/network", json=cur)
    assert r.json()["saved"] is False and "testserver" in r.json()["warnings"][0]
    assert "allowed_hosts" not in yaml.safe_load(path.read_text())
    assert c.put("/api/settings/network", json={**cur, "confirm": True}).json()["saved"] is True


def test_network_star_proxy_and_missing_known_hosts_warn(app_client):
    c, _ = app_client
    cur = _guarded(c, "network", trusted_proxies=["*"], known_hosts="/nonexistent/known_hosts")
    w = c.put("/api/settings/network", json=cur).json()["warnings"]
    assert len(w) == 2


def test_network_blank_removes_keys(app_client):
    c, path = app_client
    cur = _guarded(c, "network", public_url="http://a.lan")
    assert c.put("/api/settings/network", json=cur).status_code == 200
    cur = _guarded(c, "network", public_url="")
    assert c.put("/api/settings/network", json=cur).status_code == 200
    assert "public_url" not in yaml.safe_load(path.read_text())
