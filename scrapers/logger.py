"""
Centralized logging setup for mcscrap.

Single entry point: `setup_logging()` should be called once at program start
(typically in main.py). After that, every module just does:

    import logging
    logger = logging.getLogger(__name__)

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
# We omit the date — runs are short enough that time-of-day is sufficient,
# and the main.py "=== Run started at ... ===" banner already provides date context.
_LOG_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)-13s: %(message)s"
_DATE_FORMAT = "%H:%M:%S"

# Noisy third-party loggers — pinned to WARNING so they stay quiet even when
# our own code runs at DEBUG. Add new entries here if some library starts
# spamming the log; do NOT lower the root level to silence them.
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

    Idempotent: subsequent calls replace any existing handlers, so re-imports
    or repeated invocations during testing won't double up log output.
    """
    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)

    # Drop any pre-existing handlers (e.g. from a previous setup_logging call,
    # or from libraries that touched the root logger on import).
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    root.addHandler(handler)

    # Silence chatty libraries even when LOG_LEVEL=DEBUG. They use the standard
    # `logging` module too, so without this they'd flood the output with TLS
    # handshakes, HTTP frames, selector polling, etc.
    for name in _NOISY_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)
