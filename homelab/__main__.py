import logging
import os

import uvicorn

from .app import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# Serve HTTPS directly when given a certificate; otherwise put a TLS reverse proxy in front.
tls = (
    {"ssl_certfile": os.environ["TLS_CERT"], "ssl_keyfile": os.environ.get("TLS_KEY")}
    if os.environ.get("TLS_CERT")
    else {}
)
uvicorn.run(create_app(), host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")), **tls)
