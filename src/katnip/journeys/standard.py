from __future__ import annotations

import asyncio
import logging
import random
import time
import math

from playwright.async_api import (
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from katnip.config import ConsentConfig
from katnip.consent import ConsentManager
from katnip.interruptions import SecurityVerificationHandler
from katnip.visual_cursor import VisualCursor

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone


CaptureResetCallback = Callable[[], Awaitable[None]]
BrowserCacheClearCallback = Callable[[], Awaitable[None]]

logger = logging.getLogger("katnip.journeys.standard")


class StandardJourney:
    PAGE_LOAD_TIMEOUT_MS = 120_000
    PAGE_STABILITY_TIMEOUT_SECONDS = 15.0
    HEIGHT_STABLE_CHECKS = 4
    HEIGHT_CHECK_INTERVAL_SECONDS = 0.75

    INITIAL_WAIT_MIN_SECONDS = 2.0
    INITIAL_WAIT_MAX_SECONDS = 4.0
    FINAL_WAIT_MIN_SECONDS = 1.0
    FINAL_WAIT_MAX_SECONDS = 2.0

    SCROLL_STEP_MIN = 200
    SCROLL_STEP_MAX = 700

    TOP_STEP_MIN = 500
    TOP_STEP_MAX = 900
    
    SCROLL_PAUSE_MIN = 0.35
    SCROLL_PAUSE_MAX = 0.9

    BOTTOM_SETTLE_SECONDS = 2.0
    MAX_BOTTOM_CONFIRMATIONS = 4
    MAX_SCROLL_SECONDS = 60.0

    INTERRUPTION_CHECK_INTERVAL_SECONDS = 2.0

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
        self.run_dir = run_dir
        self.visual_cursor = visual_cursor

        self.consent = ConsentManager(
            consent_config,
            visual_cursor=visual_cursor,
        )

        self.security_verification = SecurityVerificationHandler()
        self.reset_capture = reset_capture
        self.clear_browser_cache = clear_browser_cache
            

    async def run(
        self,
        page: Page,
        website: str,
    ) -> None:
        logger.info("Loading %s", website)

        # This initial load is only used to display and interact with
        # the consent banner. Its traffic will be archived later.
        await page.goto(
            website,
            wait_until="load",
            timeout=self.PAGE_LOAD_TIMEOUT_MS,
        )

        await self.security_verification.interrupt_if_detected(
            page,
            stage="initial navigation",
        )

        await self._wait_for_page_ready(page)

        await self.security_verification.interrupt_if_detected(
            page,
            stage="page readiness",
        )

        await asyncio.sleep(
            random.uniform(
                self.INITIAL_WAIT_MIN_SECONDS,
                self.INITIAL_WAIT_MAX_SECONDS,
            )
        )

        await self.security_verification.interrupt_if_detected(
            page,
            stage="before consent handling",
        )

        consent_result = await self.consent.handle(page)

        if consent_result.handled:
            logger.info(
                "Consent action completed "
                "| provider=%s "
                "| action=%s",
                consent_result.provider,
                consent_result.action,
            )

            await self.security_verification.interrupt_if_detected(
                page,
                stage="after consent action",
            )

            if self.reset_capture is None:
                raise RuntimeError(
                    "Consent was handled, but the journey has no "
                    "network-capture reset callback."
                )

            logger.info(
                "Resetting network capture before post-consent refresh"
            )
            
            # Pre-reset reload, flush out active technologies
            await page.reload(
                wait_until="load",
                timeout=self.PAGE_LOAD_TIMEOUT_MS,
            )

            # Stops mitmproxy, archives the initial traffic, and starts a
            # fresh mitmproxy capture.
            await self.reset_capture()
            
            if self.clear_browser_cache is not None:
                logger.info(
                    "Clearing Chrome cache before post-consent reload"
                )

                await self.clear_browser_cache()

            logger.info(
                "Refreshing page with clean post-consent capture"
            )
        
            post_consent_start = datetime.now(timezone.utc).timestamp()
            # Everything from this reload forward belongs to the final report.
            await page.reload(
                wait_until="load",
                timeout=self.PAGE_LOAD_TIMEOUT_MS,
            )
            boundary_path = (
                self.run_dir
                / "mitm"
                / "post_consent_start.txt"
            )
            boundary_path.write_text(
                str(post_consent_start),
                encoding="utf-8",
            )

            await self.security_verification.interrupt_if_detected(
                page,
                stage="post-consent refresh",
            )

            await self._wait_for_page_ready(page)

            await self.security_verification.interrupt_if_detected(
                page,
                stage="post-consent page readiness",
            )

            await asyncio.sleep(
                random.uniform(1.0, 2.0)
            )

        else:
            logger.info(
                "No consent action was performed; continuing with "
                "the existing network capture"
            )

        await self._scroll_to_bottom(page)
        await self._scroll_to_top(page)

        await self.security_verification.interrupt_if_detected(
            page,
            stage="after scrolling",
        )

        await asyncio.sleep(
            random.uniform(
                self.FINAL_WAIT_MIN_SECONDS,
                self.FINAL_WAIT_MAX_SECONDS,
            )
        )

    async def _wait_for_page_ready(self, page: Page) -> None:
        try:
            await page.wait_for_load_state(
                "load",
                timeout=self.PAGE_LOAD_TIMEOUT_MS,
            )
            logger.info("Browser load event completed")
        except PlaywrightTimeoutError:
            logger.warning("Timed out waiting for browser load event")

        await self.security_verification.interrupt_if_detected(
            page,
            stage="load event wait",
        )

        try:
            await page.wait_for_function(
                "() => document.readyState === 'complete'",
                timeout=self.PAGE_LOAD_TIMEOUT_MS,
            )
            logger.info("document.readyState is complete")
        except PlaywrightTimeoutError:
            logger.warning(
                "Timed out waiting for document.readyState=complete"
            )

        await self.security_verification.interrupt_if_detected(
            page,
            stage="document ready-state wait",
        )

        deadline = time.monotonic() + self.PAGE_STABILITY_TIMEOUT_SECONDS
        previous_height: int | None = None
        stable_checks = 0
        next_interruption_check = time.monotonic()

        while time.monotonic() < deadline:
            if time.monotonic() >= next_interruption_check:
                await self.security_verification.interrupt_if_detected(
                    page,
                    stage="page-height stabilization",
                )

                next_interruption_check = (
                    time.monotonic()
                    + self.INTERRUPTION_CHECK_INTERVAL_SECONDS
                )

            height = int(
                await page.evaluate(
                    """
                    () => Math.max(
                        document.body?.scrollHeight || 0,
                        document.documentElement?.scrollHeight || 0,
                        document.body?.offsetHeight || 0,
                        document.documentElement?.offsetHeight || 0
                    )
                    """
                )
            )

            if height == previous_height:
                stable_checks += 1

                if stable_checks >= self.HEIGHT_STABLE_CHECKS:
                    logger.info(
                        "Page height stabilized at %d pixels",
                        height,
                    )
                    return
            else:
                previous_height = height
                stable_checks = 0

            await asyncio.sleep(self.HEIGHT_CHECK_INTERVAL_SECONDS)

        logger.warning(
            "Page height did not stabilize within %.1f seconds",
            self.PAGE_STABILITY_TIMEOUT_SECONDS,
        )

    async def _get_scroll_state(
        self,
        page: Page,
    ) -> dict[str, int | bool]:
        return await page.evaluate(
            """
            () => {
                const body = document.body;
                const root = document.documentElement;

                const scrollY =
                    window.scrollY ||
                    root.scrollTop ||
                    body?.scrollTop ||
                    0;

                const viewportHeight = window.innerHeight;

                const documentHeight = Math.max(
                    body?.scrollHeight || 0,
                    root?.scrollHeight || 0,
                    body?.offsetHeight || 0,
                    root?.offsetHeight || 0
                );

                return {
                    scrollY,
                    viewportHeight,
                    documentHeight,
                    atBottom:
                        scrollY + viewportHeight >= documentHeight - 5
                };
            }
            """
        )

    async def _move_mouse(self, page: Page) -> None:
        viewport = page.viewport_size

        if not viewport:
            return

        width = viewport["width"]
        height = viewport["height"]

        if width <= 100 or height <= 100:
            return

        await page.mouse.move(
            random.randint(50, width - 50),
            random.randint(50, height - 50),
            steps=random.randint(8, 24),
        )
        


    async def _smooth_wheel_scroll(
        self,
        page: Page,
        total_delta: int,
    ) -> None:
        direction = 1 if total_delta >= 0 else -1
        distance = abs(total_delta)

        steps = random.randint(18, 32)
        duration = random.uniform(0.55, 1.15)
        step_delay = duration / steps

        applied = 0

        for step in range(steps):
            progress = (step + 1) / steps

            # Smooth acceleration and deceleration.
            eased = 0.5 - 0.5 * math.cos(math.pi * progress)
            previous_progress = step / steps
            previous_eased = (
                0.5 - 0.5 * math.cos(math.pi * previous_progress)
            )

            step_delta = round(
                distance * (eased - previous_eased)
            )

            if step_delta <= 0:
                continue

            applied += step_delta

            await page.mouse.wheel(
                0,
                direction * step_delta,
            )

            await asyncio.sleep(
                max(
                    0.005,
                    step_delay
                    + random.uniform(-0.006, 0.006),
                )
            )

        remainder = distance - applied

        if remainder > 0:
            await page.mouse.wheel(
                0,
                direction * remainder,
            )

    async def _scroll_to_bottom(self, page: Page) -> None:
        deadline = time.monotonic() + self.MAX_SCROLL_SECONDS
        bottom_confirmations = 0
        next_interruption_check = time.monotonic()

        while time.monotonic() < deadline:
            if time.monotonic() >= next_interruption_check:
                await self.security_verification.interrupt_if_detected(
                    page,
                    stage="downward scrolling",
                )

                next_interruption_check = (
                    time.monotonic()
                    + self.INTERRUPTION_CHECK_INTERVAL_SECONDS
                )

            state = await self._get_scroll_state(page)

            scroll_y = int(state["scrollY"])
            viewport_height = int(state["viewportHeight"])
            document_height = int(state["documentHeight"])
            at_bottom = bool(state["atBottom"])

            if at_bottom:
                await asyncio.sleep(self.BOTTOM_SETTLE_SECONDS)

                await self.security_verification.interrupt_if_detected(
                    page,
                    stage="page bottom",
                )

                updated_state = await self._get_scroll_state(page)
                updated_height = int(updated_state["documentHeight"])

                if updated_height > document_height:
                    logger.info(
                        "Lazy-loaded content increased page height "
                        "from %d to %d",
                        document_height,
                        updated_height,
                    )
                    bottom_confirmations = 0
                    continue

                bottom_confirmations += 1

                if bottom_confirmations >= self.MAX_BOTTOM_CONFIRMATIONS:
                    logger.info(
                        "Confirmed final page bottom at %d pixels",
                        updated_height,
                    )
                    return

                continue

            bottom_confirmations = 0

            await self._move_mouse(page)

            remaining = max(
                1,
                document_height - viewport_height - scroll_y,
            )

            delta = min(
                random.randint(
                    self.SCROLL_STEP_MIN,
                    self.SCROLL_STEP_MAX,
                ),
                remaining,
            )

            await self._smooth_wheel_scroll(
                    page,
                    delta,
                )
            
            if random.random() < 0.18:
                await asyncio.sleep(random.uniform(0.4, 1.2))

            if random.random() < 0.08:
                await self._smooth_wheel_scroll(
                    page,
                    random.randint(-40, 40),
                )

            await asyncio.sleep(
                random.uniform(
                    self.SCROLL_PAUSE_MIN,
                    self.SCROLL_PAUSE_MAX,
                )
            )

        logger.warning("Maximum downward scroll duration reached")

    async def _scroll_to_top(self, page: Page) -> None:
        deadline = time.monotonic() + self.MAX_SCROLL_SECONDS
        next_interruption_check = time.monotonic()

        while time.monotonic() < deadline:
            if time.monotonic() >= next_interruption_check:
                await self.security_verification.interrupt_if_detected(
                    page,
                    stage="upward scrolling",
                )

                next_interruption_check = (
                    time.monotonic()
                    + self.INTERRUPTION_CHECK_INTERVAL_SECONDS
                )

            scroll_y = int(
                await page.evaluate(
                    """
                    () => (
                        window.scrollY ||
                        document.documentElement.scrollTop ||
                        document.body?.scrollTop ||
                        0
                    )
                    """
                )
            )

            if scroll_y <= 1:
                await page.evaluate("() => window.scrollTo(0, 0)")
                logger.info("Returned to top of page")
                return

            await self._move_mouse(page)

            delta = min(
                random.randint(
                    self.TOP_STEP_MIN,
                    self.TOP_STEP_MAX,
                ),
                scroll_y,
            )

            await self._smooth_wheel_scroll(
                page,
                -delta,
            )

            await asyncio.sleep(
                random.uniform(
                    self.SCROLL_PAUSE_MIN,
                    self.SCROLL_PAUSE_MAX,
                )
            )

        logger.warning(
            "Maximum upward scroll duration reached; forcing top"
        )

        await page.evaluate(
            """
            () => window.scrollTo({
                top: 0,
                left: 0,
                behavior: "instant"
            })
            """
        )