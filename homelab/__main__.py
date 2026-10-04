import logging
import os

import uvicorn

from .app import create_app


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO, and a Telegram URL carries the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    configure_logging()
    uvicorn.run(create_app(), host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
