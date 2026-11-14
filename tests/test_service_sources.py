import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from fleetdeck.app import create_app
from fleetdeck.config import AppConfig, ServicesConfig, ServiceSource, SSHConfig
from fleetdeck.models import CheckResult, Target
from fleetdeck.service_sources import SSH_SCRIPT, SourceCache, parse_api, parse_inspect, source_target
from fleetdeck.services import build_services, native_entries

NOW = datetime.now(UTC)


def inspect_output(*containers, nets="bridge bridge"):
    return f"__DOCKER__=yes\n__ND__ {nets}\n__INSPECT__\n{json.dumps(list(containers))}\n"


JELLY = {
    "Id": "abc123",
    "Name": "/jellyfin",
    "Config": {
        "Image": "jellyfin/jellyfin",
        "Labels": {"net.unraid.docker.webui": "http://[IP]:[PORT:8096]/web"},
        "ExposedPorts": {"8096/tcp": {}},
    },
    "State": {"Status": "running"},
    "HostConfig": {"NetworkMode": "bridge"},
    "NetworkSettings": {
        "Ports": {"8096/tcp": [{"HostIp": "0.0.0.0", "HostPort": "18096"}]},
        "Networks": {"bridge": {"IPAddress": "172.17.0.2"}},
    },
}


def test_script_is_read_only():
    for word in ("rm ", "pull", "restart", "stop", "exec", "run ", "update", "kill", "prune"):
        assert word not in SSH_SCRIPT


def test_inspect_parses_unraid_webui_label_with_mapped_port():
    result = parse_inspect(inspect_output(JELLY), "nas.lan")
    svc = result.standalone[0]
    assert svc.name == "jellyfin" and svc.status == "running"
    assert svc.explicit_urls == ["http://nas.lan:18096/web"]
    assert svc.ports[0].host_port == 18096


def test_inspect_survives_garbage_and_missing_docker():
    assert parse_inspect("__DOCKER__=none\n", "nas").error
    assert parse_inspect("__DOCKER__=yes\n__INSPECT__\nnot json", "nas").error
    assert parse_inspect(inspect_output("junk", 5), "nas").standalone == []


def test_unraid_label_with_bad_scheme_is_dropped():
    bad = {**JELLY, "Config": {**JELLY["Config"], "Labels": {"net.unraid.docker.webui": "javascript:alert(1)"}}}
    assert parse_inspect(inspect_output(bad), "nas").standalone[0].explicit_urls == []


def test_api_parse():
    containers = [
        {
            "Id": "x",
            "Names": ["/sonarr"],
            "Image": "sonarr",
            "State": "running",
            "Labels": {},
            "Ports": [{"IP": "0.0.0.0", "PrivatePort": 8989, "PublicPort": 8989, "Type": "tcp"}, {"PrivatePort": 1}],
            "NetworkSettings": {"Networks": {"br0": {"IPAddress": "192.168.1.5"}}},
        }
    ]
    result = parse_api(containers, [{"Name": "br0", "Driver": "macvlan"}], "nas.lan")
    svc = result.standalone[0]
    assert [p.host_port for p in svc.ports] == [8989]
    assert svc.networks[0].driver == "macvlan"


def test_source_cards_are_link_only_and_not_in_inventory():
    source = ServiceSource(name="nas", address="nas.lan", ssh=SSHConfig(host="10.0.0.9"))
    cache = SourceCache([source])
    cache.results["nas"] = parse_inspect(inspect_output(JELLY), "nas.lan")
    view = build_services(cache.targets, cache.stacks, ServicesConfig(), set(), sources=cache.status_models())
    entry = view.services[0]
    assert entry.link_only and entry.id.startswith("src:nas|")
    assert entry.links[0].url == "http://nas.lan:18096/web"
    assert view.sources[0].scanned
    assert source_target(source).updatable is False


def test_source_validation():
    with pytest.raises(ValueError):
        ServiceSource(name="x", type="ssh")
    with pytest.raises(ValueError):
        ServiceSource(name="x", type="docker-api")


async def test_scan_failure_is_recorded_not_raised(monkeypatch):
    async def boom(source):
        raise OSError("no route")

    monkeypatch.setattr("fleetdeck.service_sources.scan_source", boom)
    cache = SourceCache([ServiceSource(name="nas", type="docker-api", url="http://nas:2375")])
    await cache.scan(force=True)
    assert "no route" in cache.status_models()[0].error


def test_native_helper_script_apps():
    target = Target(id="pve:plex", name="plex", platform="proxmox", source="pve", kind="lxc", address="plex.lan")
    check = CheckResult(target_id="pve:plex", checked_at=NOW, community_script="plex")
    (entry,) = native_entries({target.id: target}, {target.id: check}, ServicesConfig(), set())
    assert entry.links[0].url == "http://plex.lan:32400/web"
    assert native_entries({target.id: target}, {}, ServicesConfig(), set()) == []


def test_add_and_remove_manual_link(tmp_path):
    cfg = AppConfig(demo=True, state_file=str(tmp_path / "s.db"))
    with TestClient(create_app(cfg)) as client:
        bad = client.post("/api/services/links", json={"name": "x", "url": "javascript:alert(1)"})
        assert bad.status_code == 400
        view = client.post("/api/services/links", json={"name": "Router", "url": "http://192.168.1.1"}).json()
        item = next(s for s in view["services"] if s["name"] == "Router")
        assert item["manual"] and item["links"][0]["url"] == "http://192.168.1.1"
        demo_nas = [s for s in view["services"] if s["link_only"] and s["machine"] == "nas"]
        assert demo_nas
    with TestClient(create_app(cfg)) as client:  # survives a restart
        view = client.get("/api/services").json()
        item = next(s for s in view["services"] if s["name"] == "Router")
        gone = client.delete(f"/api/services/links/{item['id'].split('|')[1]}").json()
        assert all(s["name"] != "Router" for s in gone["services"])
        assert client.delete("/api/services/links/nope").status_code == 404
        assert client.post("/api/services/scan").status_code == 200  # demo source never connects
