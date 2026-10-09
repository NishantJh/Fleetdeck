import logging
import os

import uvicorn

from .app import create_app
from .config import AppConfig, config_path, load_config

log = logging.getLogger("homelab")
APP_UID = 10001


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO, and a Telegram URL carries the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def server_options(cfg: AppConfig) -> dict:
    opts: dict = {
        "host": os.environ.get("HOST", "0.0.0.0"),
        "port": int(os.environ.get("PORT", "8080")),
        # Only these peers may say who the real client is (X-Forwarded-For/-Proto).
        "proxy_headers": True,
        "forwarded_allow_ips": ",".join(cfg.trusted_proxies) or "127.0.0.1",
    }
    # Serve HTTPS directly when given a certificate; otherwise put a TLS reverse proxy in front.
    if os.environ.get("TLS_CERT"):
        opts.update(ssl_certfile=os.environ["TLS_CERT"], ssl_keyfile=os.environ.get("TLS_KEY"))
    return opts


def drop_privileges() -> None:
    """In the container: hand the data volume to the app user, then stop being root.

    The image starts as root only for this. A bind-mounted folder arrives owned by whoever created
    it on the host, so the app fixes the ownership itself instead of asking for a `chown`. After
    switching user the process keeps no capabilities. Only runs when HOMELAB_DATA is set (the image
    sets it), so running from a checkout as root is left alone.
    """
    data = os.environ.get("HOMELAB_DATA")
    if not data or not hasattr(os, "getuid") or os.getuid() != 0:
        return
    uid = int(os.environ.get("PUID", APP_UID))
    gid = int(os.environ.get("PGID", uid))
    if uid == 0:
        log.warning("PUID=0: running as root.")
        return
    os.makedirs(data, exist_ok=True)
    for root, dirs, files in os.walk(data):
        for path in [root, *(os.path.join(root, n) for n in dirs + files)]:
            try:
                st = os.lstat(path)
                if (st.st_uid, st.st_gid) != (uid, gid):
                    os.lchown(path, uid, gid)
            except OSError as e:  # e.g. rootless Docker can't map the uid; the app reports what it can't read
                log.warning("Can't change the owner of %s: %s", path, e)
    os.chmod(data, 0o700)
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    os.environ["HOME"] = data


def serve(path: str) -> bool:
    """Run the dashboard until it stops; True if it stopped to restart with a changed config."""
    cfg = load_config(path)
    restarting = False

    def restart() -> None:
        nonlocal restarting
        restarting = True
        server.should_exit = True

    app = create_app(cfg, config_file=path, restart=restart)
    server = uvicorn.Server(uvicorn.Config(app, **server_options(cfg)))
    server.run()
    return restarting


def main() -> None:
    configure_logging()
    drop_privileges()
    path = config_path()
    # The setup page saves the config and asks for a restart; the new config is loaded in-process.
    while serve(path):
        log.info("Restarting with the new configuration.")


if __name__ == "__main__":
    main()
