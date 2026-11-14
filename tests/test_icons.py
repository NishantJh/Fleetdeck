import httpx
import pytest

from fleetdeck.icons import IconCache, icon_slug, valid_slug

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"/>'
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 10


@pytest.mark.parametrize(
    ("image", "name", "slug"),
    [
        ("lscr.io/linuxserver/radarr:latest", "", "radarr"),
        ("ghcr.io/immich-app/immich-server:release", "", "immich"),
        ("vaultwarden/server:1.32", "", "vaultwarden"),
        ("plexinc/pms-docker", "", "plex"),
        ("adguard/adguardhome@sha256:" + "a" * 64, "", "adguard-home"),
        ("localhost:5000/app:dev", "", "app"),
        ("", "Home Assistant", "home-assistant"),
        ("installed on the machine (not Docker)", "pi-hole", "pi-hole"),
        ("", "../../etc", "etc"),
        ("", "!!!", ""),
    ],
)
def test_icon_slug(image, name, slug):
    assert icon_slug(image, name) == slug


def test_valid_slug_rejects_paths():
    assert valid_slug("plex") and not valid_slug("../plex") and not valid_slug("Plex") and not valid_slug("a.svg")


def client(routes, calls):
    def handler(request):
        calls.append(request.url.path)
        status, body = routes.get(
            request.url.path.rsplit("/", 2)[-2] + "/" + request.url.path.rsplit("/", 1)[-1], (404, b"")
        )
        return httpx.Response(status, content=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_downloads_once_and_serves_from_cache(tmp_path):
    calls = []
    cache = IconCache(tmp_path, client=client({"svg/plex.svg": (200, SVG)}, calls))
    path, media = await cache.get("plex")
    assert media == "image/svg+xml" and path.read_bytes() == SVG
    await cache.get("plex")
    assert len(calls) == 1


async def test_png_fallback_and_missing_marker(tmp_path):
    calls = []
    cache = IconCache(tmp_path, client=client({"png/foo.png": (200, PNG)}, calls))
    assert (await cache.get("foo"))[1] == "image/png"
    assert await cache.get("nothing") is None
    assert len(list(tmp_path.glob("*.missing"))) == 1
    before = len(calls)
    assert await cache.get("nothing") is None
    assert len(calls) == before  # remembered, not asked again


async def test_rejects_non_images_and_disabled(tmp_path):
    calls = []
    cache = IconCache(tmp_path, client=client({"svg/bad.svg": (200, b"<html>")}, calls))
    assert await cache.get("bad") is None and not list(tmp_path.iterdir())
    off = IconCache(tmp_path, enabled=False, client=client({"svg/plex.svg": (200, SVG)}, calls))
    before = len(calls)
    assert await off.get("plex") is None and len(calls) == before


async def test_offline_leaves_no_marker(tmp_path):
    def boom(request):
        raise httpx.ConnectError("offline")

    cache = IconCache(tmp_path, client=httpx.AsyncClient(transport=httpx.MockTransport(boom)))
    assert await cache.get("plex") is None
    assert not list(tmp_path.glob("*.missing"))


def test_icon_route_validates_and_sandboxes(tmp_path):
    from fastapi.testclient import TestClient

    from fleetdeck.app import create_app
    from fleetdeck.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text("demo: true\nstate_file: state.db\nservices:\n  icons: false\n")
    IconCache(tmp_path / "icons")._file("plex", "svg").parent.mkdir()
    IconCache(tmp_path / "icons")._file("plex", "svg").write_bytes(SVG)
    with TestClient(create_app(load_config(str(path)), config_file=str(path))) as c:
        r = c.get("/api/icons/plex")
        assert r.status_code == 200 and r.headers["content-type"] == "image/svg+xml"
        assert "sandbox" in r.headers["content-security-policy"]
        assert c.get("/api/icons/jellyfin").status_code == 404  # downloads off: cached icons only
        assert c.get("/api/icons/..%2Fconfig.yaml").status_code == 404
