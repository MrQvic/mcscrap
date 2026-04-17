"""
The level is read from the LOG_LEVEL env var (DEBUG / INFO / WARNING / ERROR).
Defaults to INFO for normal operation; switch to DEBUG when investigating
flow issues in the scrapers.
"""
import logging
import sys

from .config import LOG_LEVEL

# Format: HH:MM:SS [LEVEL  ] logger.name  : message
# - levelname is left-padded to 7 chars (longest standard level is "WARNING")
# - name is left-padded to 13 chars (longest current logger name is "mc.czechcraft")
_LOG_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)-13s: %(message)s"
_DATE_FORMAT = "%H:%M:%S"
_NOISY_LIBRARIES = (
    "httpx",
    "httpcore",
    "asyncio",
    "urllib3",
    "websockets",
    "playwright",
    "patchright",
)


def setup_logging() -> None:
    """
    Configure the root logger with a single stdout handler.
    """
    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    root.addHandler(handler)

    # Silence libraries even when LOG_LEVEL=DEBUG.
    for name in _NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)
