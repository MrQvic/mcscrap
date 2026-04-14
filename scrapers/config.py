import os

from dotenv import load_dotenv

load_dotenv()


def _get_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"Environment variable {key} must be an integer, got: {raw!r}")


# How long to wait for NopeCHA (or a human in debug mode) to solve a captcha, in milliseconds.
CAPTCHA_TIMEOUT_MS: int = _get_int("CAPTCHA_TIMEOUT_MS", 30_000)

# Timeout for HTTP requests (get_vote_info phase), in seconds.
HTTP_TIMEOUT_S: int = _get_int("HTTP_TIMEOUT_S", 15)
