import logging
import os

import uvicorn

from .app import create_app
from .config import AppConfig, load_config


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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    uvicorn.run(create_app(cfg), **server_options(cfg))


if __name__ == "__main__":
    main()
