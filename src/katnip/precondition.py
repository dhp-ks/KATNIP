from __future__ import annotations

import asyncio
import logging

from playwright.async_api import (
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from katnip.config import PreconditionConfig


logger = logging.getLogger("katnip.precondition")


class BrowserPreconditioner:
    PAGE_LOAD_TIMEOUT_MS = 60_000

    def __init__(
        self,
        config: PreconditionConfig,
    ) -> None:
        self.config = config

    async def run(
        self,
        page: Page,
    ) -> None:
        if not self.config.enabled:
            logger.info(
                "Browser preconditioning disabled"
            )
            return

        if not self.config.sites:
            logger.info(
                "Browser preconditioning enabled "
                "but no sites configured"
            )
            return

        logger.info(
            "Starting browser preconditioning "
            "| site_count=%d",
            len(self.config.sites),
        )

        for index, site in enumerate(
            self.config.sites,
            start=1,
        ):
            logger.info(
                "Preconditioning site %d/%d "
                "| url=%s",
                index,
                len(self.config.sites),
                site.url,
            )

            try:
                await page.goto(
                    site.url,
                    wait_until="load",
                    timeout=self.PAGE_LOAD_TIMEOUT_MS,
                )

            except PlaywrightTimeoutError:
                logger.warning(
                    "Preconditioning page did not reach "
                    "load state within timeout "
                    "| url=%s",
                    site.url,
                )

            # Allow client-side scripts, tags and cookie writes
            # to execute after the normal load event.
            if site.wait_seconds > 0:
                await asyncio.sleep(
                    site.wait_seconds
                )

        logger.info(
            "Browser preconditioning completed"
        )

        # Leave the browser on a neutral page so the target
        # journey controls the next navigation.
        await page.goto(
            "about:blank",
            wait_until="load",
        )
