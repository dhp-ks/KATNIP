from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from playwright.async_api import Page

from katnip.config import ConsentConfig
from katnip.consent import ConsentManager
from katnip.interruptions import SecurityVerificationHandler
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from katnip.visual_cursor import VisualCursor


CaptureResetCallback = Callable[[], Awaitable[None]]
BrowserCacheClearCallback = Callable[[], Awaitable[None]]

logger = logging.getLogger("katnip.journeys.manual")


class ManualJourney:
    PAGE_LOAD_TIMEOUT_MS = 120_000
    POLL_INTERVAL_SECONDS = 0.5

    def __init__(
        self,
        journey_settings,
        consent_config: ConsentConfig,
        run_dir: Path,
        reset_capture: CaptureResetCallback | None = None,
        clear_browser_cache: BrowserCacheClearCallback | None = None,
        visual_cursor: VisualCursor | None = None,
    ) -> None:
        self.journey_settings = journey_settings
        self.consent = ConsentManager(
            consent_config,
            visual_cursor=visual_cursor,
        )
        self.security_verification = SecurityVerificationHandler()
        self.run_dir = run_dir
        self.reset_capture = reset_capture
        self.clear_browser_cache = clear_browser_cache

        self.stop_file = self.run_dir / "STOP_MANUAL_JOURNEY"
    
    async def run(
        self,
        page: Page,
        website: str,
    ) -> None:
        logger.info(
            "Starting manual journey | website=%s",
            website,
        )

        if self.stop_file.exists():
            self.stop_file.unlink()

        await page.goto(
            website,
            wait_until="load",
            timeout=self.PAGE_LOAD_TIMEOUT_MS,
        )

        await self.security_verification.interrupt_if_detected(
            page,
            stage="manual journey initial navigation",
        )

        consent_result = await self.consent.handle(page)

        if consent_result.handled:
            logger.info(
                "Consent action completed | provider=%s | action=%s",
                consent_result.provider,
                consent_result.action,
            )

            await self.security_verification.interrupt_if_detected(
                page,
                stage="after consent action",
            )

            if self.reset_capture is None:
                raise RuntimeError(
                    "Consent was handled, but the manual journey has no "
                    "network-capture reset callback."
                )

            # Flush activity caused by the consent choice before splitting captures.
            await page.reload(
                wait_until="load",
                timeout=self.PAGE_LOAD_TIMEOUT_MS,
            )

            await self.reset_capture()

            if self.clear_browser_cache is not None:
                await self.clear_browser_cache()

            # The analyst starts from a fresh post-consent page load.
            post_consent_start = datetime.now(timezone.utc).timestamp()
            await page.reload(
                wait_until="load",
                timeout=self.PAGE_LOAD_TIMEOUT_MS,
            )

            boundary_path = self.run_dir / "mitm" / "post_consent_start.txt"
            boundary_path.write_text(
                str(post_consent_start),
                encoding="utf-8",
            )

            await self.security_verification.interrupt_if_detected(
                page,
                stage="manual journey post-consent refresh",
            )
        else:
            logger.info(
                "No consent action was performed; continuing with the "
                "existing network capture"
            )

        # Keep the existing ready log and stop-file polling loop below this point.

        logger.info(
            "Manual journey ready. "
            "Interact with Chrome manually."
        )

        logger.info(
            "To stop the manual journey, create: %s",
            self.stop_file,
        )

        while True:
            if self.stop_file.exists():
                logger.info(
                    "Manual journey stop signal detected"
                )

                try:
                    self.stop_file.unlink()
                except OSError:
                    pass

                return

            if page.is_closed():
                logger.info(
                    "Manual journey ended because the page was closed"
                )
                return

            await asyncio.sleep(
                self.POLL_INTERVAL_SECONDS
            )