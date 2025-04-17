from __future__ import annotations

import logging
import sys

from core.bot import create_app
from core.config import load_config
from core.db import init_db


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        stream=sys.stdout,
    )

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("starlette").setLevel(logging.WARNING)


config = load_config()
_configure_logging(config.log_level)

init_db(config.database_url)

app = create_app(config)

if __name__ == "__main__":
    try:
        import uvicorn
    except Exception:
        logging.error(
            "uvicorn is required to run the server directly. "
            "Install with: pip install 'uvicorn[standard]'"
        )
        raise
    logging.info("Starting uvicorn on %s:%s", config.server_host, config.server_port)
    uvicorn.run(
        app,
        host=config.server_host,
        port=config.server_port,
        log_level=config.log_level.lower(),
    )
