import os
import tempfile
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from patchright.sync_api import BrowserContext, Playwright

load_dotenv()

# Path to the unpacked Nopecha extension directory (must contain manifest.json)
NOPECHA_EXTENSION_PATH = Path(__file__).parent.parent / "extensions" / "nopecha"

# Persistent Chrome profile dir - reused across runs so extension state survives
CHROME_PROFILE_DIR = Path(__file__).parent.parent / ".chrome_profile"


class BrowserManager:
    """
    Context manager that sets up a Chromium BrowserContext with:
      - Xvfb virtual display (when DEBUG=false)
      - Nopecha extension loaded and API key injected
      - headless=False (required for extensions to work)

    Usage:
        with sync_playwright() as p:
            with BrowserManager(p) as context:
                page = context.new_page()
                ...

    Debug mode (DEBUG=true in .env):
        Browser window appears on your real display instead of Xvfb.
    """

    def __init__(self, playwright: Playwright):
        self.playwright = playwright
        self._display = None
        self.context: Optional[BrowserContext] = None

    def __enter__(self) -> BrowserContext:
        debug = os.getenv("DEBUG", "false").lower() == "true"
        nopecha_enabled = os.getenv("NOPECHA_ENABLED", "true").lower() == "true"

        if nopecha_enabled:
            api_key = os.getenv("NOPECHA_API_KEY")
            if not api_key:
                raise EnvironmentError("NOPECHA_API_KEY is not set. Check your .env file.")

            if not NOPECHA_EXTENSION_PATH.exists():
                raise FileNotFoundError(
                    f"Nopecha extension not found at: {NOPECHA_EXTENSION_PATH}\n"
                    "Download from https://github.com/nopecha/nopecha-chrome and unpack there."
                )

        if not debug:
            # Start virtual framebuffer - browser renders into it invisibly
            from xvfbwrapper import Xvfb
            self._display = Xvfb(width=1280, height=720, colordepth=24)
            self._display.start()
            # xvfbwrapper already sets os.environ["DISPLAY"], but we re-assert it
            # explicitly and also pass it via the env= parameter on launch below,
            # because Playwright's Node driver does not reliably inherit it on Wayland/KDE.
            os.environ["DISPLAY"] = f":{self._display.new_display}"

        CHROME_PROFILE_DIR.mkdir(exist_ok=True)

        launch_args = [
            "--no-sandbox",
            "--ozone-platform=x11",  # Force X11 backend so Xvfb is respected (Wayland would bypass it)
        ]
        if nopecha_enabled:
            launch_args += [
                f"--disable-extensions-except={NOPECHA_EXTENSION_PATH}",
                f"--load-extension={NOPECHA_EXTENSION_PATH}",
            ]

        # Explicit env for the Chromium subprocess. Playwright's Node driver does not
        # reliably propagate DISPLAY from os.environ in our setup (Wayland session with
        # XWayland on :0), so we pass it explicitly to guarantee Chromium connects to Xvfb.
        child_env = {**os.environ}

        self.context = self.playwright.chromium.launch_persistent_context(
            str(CHROME_PROFILE_DIR),
            channel="chromium",  # Must use patchright's Chromium, not Chrome — Google removed
            # the --load-extension flag from Chrome 137+, breaking extension side-loading.
            # Patchright's stealth patches still apply and should pass Turnstile.
            headless=False,  # Extensions require a real (or virtual) display
            args=launch_args,
            env=child_env,
        )

        if nopecha_enabled:
            self._inject_api_key(api_key)

        return self.context

    def __exit__(self, *_):
        if self.context:
            self.context.close()
        if self._display:
            self._display.stop()

    def _inject_api_key(self, api_key: str) -> None:
        """
        Injects the Nopecha API key using their official setup URL.
        The extension reads the key from the URL hash and saves it to storage.
        """
        page = self.context.new_page()
        try:
            page.goto(
                f"https://nopecha.com/setup#{api_key}",
                wait_until="load",
                timeout=3_000,
            )
            # Give the content script time to read the hash and persist it to chrome.storage.
            # domcontentloaded/load fire before async storage writes complete, so we need to wait.
            page.wait_for_timeout(1000)
        finally:
            page.close()
