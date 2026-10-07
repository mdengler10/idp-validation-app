"""Configure app + IDP logging to console and data/logs/idp-validation.log."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "data" / "logs"
LOG_FILE = LOG_DIR / "idp-validation.log"

_CONFIGURED = False


class RequestIdFilter(logging.Filter):
    """Inject request_id from context into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            from app.request_context import get_request_id

            record.request_id = get_request_id() or "-"
        except Exception:
            record.request_id = "-"
        return True


def configure_logging(level: int | None = None) -> Path:
    global _CONFIGURED
    if _CONFIGURED:
        return LOG_FILE

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_level = level or logging.INFO
    if (sys.argv and "uvicorn" in " ".join(sys.argv)) or __name__ == "__main__":
        pass
    env_level = __import__("os").environ.get("IDP_LOG_LEVEL", "").strip().upper()
    if env_level in ("DEBUG", "INFO", "WARNING", "ERROR"):
        log_level = getattr(logging, env_level)

    fmt = (
        "%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s"
    )
    formatter = logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S")

    root = logging.getLogger()
    root.setLevel(log_level)

    req_filter = RequestIdFilter()

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    console.addFilter(req_filter)
    root.addHandler(console)

    try:
        file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.addFilter(req_filter)
        root.addHandler(file_handler)
        log_target = str(LOG_FILE)
    except OSError as file_err:
        log_target = f"(file logging disabled: {file_err})"
        logging.getLogger(__name__).warning(
            "Could not write log file %s — logging to console only: %s",
            LOG_FILE,
            file_err,
        )

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)

    logging.getLogger("app").setLevel(log_level)
    logging.getLogger("app.idp_client").setLevel(log_level)

    _CONFIGURED = True
    logging.getLogger(__name__).info(
        "Logging configured level=%s file=%s",
        logging.getLevelName(log_level),
        log_target,
    )
    return LOG_FILE
