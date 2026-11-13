from __future__ import annotations

import asyncio
import logging
import os
import shlex
import signal
import subprocess
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from katnip.config import BrowserConfig
from katnip.process import wait_for_tcp_port
from katnip.visual_cursor import (
    CURSOR_INSTALL_SCRIPT,
    VisualCursor,
)


class BrowserError(RuntimeError):
    pass


class BrowserManager:
    def __init__(self, config: BrowserConfig, run_dir: Path, log_path: Path, window_marker: str,) -> None:
        self.config = config
        self.run_dir = run_dir
        self.log_path = log_path
        self.profile_dir = run_dir / "chrome-profile"
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.launched = False
        self.logger = logging.getLogger("katnip.browser")
        self.window_marker = window_marker
        self.cdp_session = None
        self.visual_cursor = VisualCursor()

    @property
    def cdp_url(self) -> str:
        return (
            f"http://{self.config.remote_debugging_host}:"
            f"{self.config.remote_debugging_port}"
        )

    def launch(self) -> None:
        args = [
            f"--remote-debugging-address={self.config.remote_debugging_host}",
            f"--remote-debugging-port={self.config.remote_debugging_port}",
        ]
        if self.config.clean_profile:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            args.append(f"--user-data-dir={self.profile_dir}")
            
        args.extend([*self.config.launch_extra_args, f"data:text/html,<title>{self.window_marker}</title>"])
        shell_command = f"{self.config.launch_alias} {shlex.join(args)}"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.logger.info("Launching Chrome through alias %s", self.config.launch_alias)

        result = subprocess.run(
            [self.config.shell, "-lic", shell_command],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"$ {shell_command}\n")
            if result.stdout:
                handle.write(result.stdout)
            if result.stderr:
                handle.write(result.stderr)

        if result.returncode != 0:
            raise BrowserError(
                f"Chrome alias command failed with code {result.returncode}; see {self.log_path}"
            )

        wait_for_tcp_port(
            self.config.remote_debugging_host,
            self.config.remote_debugging_port,
            self.config.startup_timeout_seconds,
        )
        self.launched = True

    async def connect(self) -> Page:
        self.playwright = await async_playwright().start()

        try:
            self.browser = await self.playwright.chromium.connect_over_cdp(
                self.cdp_url
            )
        except Exception as exc:
            await self.playwright.stop()
            self.playwright = None

            raise BrowserError(
                f"Unable to connect to Chrome CDP at {self.cdp_url}: {exc}"
            ) from exc

        if self.browser.contexts:
            self.context = self.browser.contexts[0]
        else:
            self.context = await self.browser.new_context()
            
        await self.context.add_init_script(
            script=CURSOR_INSTALL_SCRIPT
        )

        if self.context.pages:
            self.page = self.context.pages[0]
        else:
            self.page = await self.context.new_page()
            
        await self.visual_cursor.install(
            self.page
        )

        #
        # Create a CDP session for this page.
        #
        self.cdp_session = await self.context.new_cdp_session(
            self.page
        )

        await self.cdp_session.send(
            "Network.enable"
        )

        #
        # Ignore Chrome's normal HTTP cache.
        #
        await self.cdp_session.send(
            "Network.setCacheDisabled",
            {
                "cacheDisabled": True,
            },
        )

        #
        # Prevent Service Workers from intercepting requests.
        #
        await self.cdp_session.send(
            "Network.setBypassServiceWorker",
            {
                "bypass": True,
            },
        )

        self.logger.info(
            "Chrome network cache disabled and Service Workers bypassed"
        )

        await self.page.bring_to_front()

        return self.page
    
    async def clear_cache(self) -> None:
        if self.cdp_session is None:
            raise BrowserError(
                "Cannot clear Chrome cache before CDP is connected"
            )

        await self.cdp_session.send(
            "Network.clearBrowserCache"
        )

        self.logger.info(
            "Chrome browser cache cleared"
        )

    async def close(self) -> None:
        if self.page is not None and not self.page.is_closed():
            try:
                session = await self.context.new_cdp_session(self.page) if self.context else None
                if session is not None:
                    await session.send("Browser.close")
                    await asyncio.sleep(0.5)
            except Exception as exc:
                self.logger.debug("CDP browser close failed: %s", exc)

        if self.playwright is not None:
            try:
                await self.playwright.stop()
            except Exception as exc:
                self.logger.debug("Playwright shutdown failed: %s", exc)

        if self.config.close_processes_on_exit:
            self._terminate_matching_chrome_processes()

    def _terminate_matching_chrome_processes(self) -> None:
        pattern = f"--remote-debugging-port={self.config.remote_debugging_port}"
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in result.stdout.splitlines():
            try:
                pid = int(line.strip())
            except ValueError:
                continue
            if pid == os.getpid():
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
