import logging
import logging.config


def configure_logging() -> None:
    """
    Single authoritative logging setup for the whole application.
    Call once from main.py at startup — all modules just use logging.getLogger(name).
    """
    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "[%(levelname)s] [%(name)s] %(message)s",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "standard",
                "stream": "ext://sys.stdout",
            },
        },
        "root": {
            "level": "DEBUG",
            "handlers": ["console"],
        },
    })
