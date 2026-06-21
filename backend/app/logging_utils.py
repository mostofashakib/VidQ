"""
Centralised logging helpers.

All modules should prefer `get_logger(__name__)` over calling
`logging.getLogger` directly — this gives us one place to configure
formatting, handlers, or structured-log adapters in the future without
touching every file.

Use `log_suppressed` instead of bare `except: pass` so that intentionally
swallowed errors remain visible in the log stream.
"""
import logging
from typing import Literal

LogLevel = Literal["debug", "info", "warning", "error", "critical"]


def get_logger(name: str) -> logging.Logger:
    """Return a named logger. Call with __name__ in every module."""
    return logging.getLogger(name)


def log_suppressed(
    logger: logging.Logger,
    msg: str,
    exc: BaseException | None = None,
    *,
    level: LogLevel = "warning",
) -> None:
    """Log an exception that is being intentionally suppressed.

    Replaces bare `except: pass` patterns so failures remain visible:

        try:
            risky_call()
        except SomeError as e:
            log_suppressed(logger, "could not do X", e)

    The log record includes the exception type and message so developers
    can reproduce the error without having to add temporary print() calls.
    Traceback is omitted by default to avoid log spam for expected failures;
    pass level="error" for unexpected ones where a full traceback is useful.
    """
    log_fn = getattr(logger, level, logger.warning)
    if exc is not None:
        log_fn("%s — %s: %s", msg, type(exc).__name__, exc)
    else:
        log_fn(msg)
