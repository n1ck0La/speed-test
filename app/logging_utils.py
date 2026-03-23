from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from app.config import LOG_DIR, Settings


LOG_FILE = LOG_DIR / "app.log"


def configure_logging(settings: Settings) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    console_exists = any(
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, RotatingFileHandler)
        for handler in root.handlers
    )
    if not console_exists:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)

    for handler in list(root.handlers):
        if isinstance(handler, RotatingFileHandler) and getattr(handler, "baseFilename", "") == str(LOG_FILE):
            root.removeHandler(handler)
            handler.close()

    rotating = RotatingFileHandler(
        LOG_FILE,
        maxBytes=settings.log_max_mb * 1024 * 1024,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    rotating.setFormatter(formatter)
    root.addHandler(rotating)
