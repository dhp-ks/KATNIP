from __future__ import annotations

import asyncio
import logging
import random
import time
from urllib.parse import urljoin, urlsplit

from playwright.async_api import (
    Error as PlaywrightError,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from katnip.config import ConsentConfig
from katnip.journeys.standard import StandardJourney
from katnip.visual_cursor import VisualCursor


logger = logging.getLogger("katnip.journeys.click")


class ClickJourney(StandardJourney):
    """
    Performs the standard journey while opening eligible links in temporary
    new tabs.

    The original page remains open and is never intentionally navigated away.
    Each child tab is observed briefly and then closed.
    """

    MAX_LINKS_PER_RUN = 5
    MAX_LINKS_PER_VIEWPORT = 1

    CHILD_PAGE_LOAD_TIMEOUT_MS = 30_000
    CHILD_PAGE_WAIT_MIN_SECONDS = 1.5
    CHILD_PAGE_WAIT_MAX_SECONDS = 3.0

    SKIP_TEXT_TERMS = (
        "log in",
        "login",
        "sign in",
        "sign out",
        "logout",
        "delete",
        "remove",
        "unsubscribe",
        "submit",
        "purchase",
        "buy now",
        "checkout",
        "add to cart",
        "download",
        "print",
    )

    SKIP_PATH_TERMS = (
        "/login",
        "/logout",
        "/signin",
        "/signout",
        "/checkout",
        "/cart",
        "/unsubscribe",
        "/delete",
        "/download",
    )

    SKIP_SCHEMES = (
        "mailto",
        "tel",
        "sms",
        "javascript",
        "data",
        "blob",
    )

    def __init__(
        self,
        journey_settings,
        consent_config: ConsentConfig,
        *,
        reset_capture=None,
        clear_browser_cache=None,
        visual_cursor: VisualCursor | None = None,
    ) -> None:
        super().__init__(
            journey_settings,
            consent_config,
            reset_capture=reset_capture,
            clear_browser_cache=clear_browser_cache,
            visual_cursor=visual_cursor,
        )

        self.clicked_urls: set[str] = set()
        self.total_links_opened = 0

    async def run(
        self,
        page: Page,
        website: str,
    ) -> None:
        logger.info("Loading %s", website)

        await page.goto(
            website,
            wait_until="load",
            timeout=self.PAGE_LOAD_TIMEOUT_MS,
        )

        if hasattr(self, "security_verification"):
            await self.security_verification.interrupt_if_detected(
                page,
                stage="initial navigation",
            )

        await self._wait_for_page_ready(page)

        if hasattr(self, "security_verification"):
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

        if hasattr(self, "security_verification"):
            await self.security_verification.interrupt_if_detected(
                page,
                stage="before consent handling",
            )

        consent_result = await self.consent.handle(page)

        if consent_result.handled:
            logger.info(
                "Consent action completed | provider=%s | action=%s",
                consent_result.provider,
                consent_result.action,
            )

            if hasattr(self, "security_verification"):
                await self.security_verification.interrupt_if_detected(
                    page,
                    stage="after consent action",
                )

            if self.reset_capture is not None:
                logger.info(
                    "Resetting network capture before post-consent reload"
                )
                await self.reset_capture()

            if self.clear_browser_cache is not None:
                logger.info(
                    "Clearing Chrome cache before post-consent reload"
                )
                await self.clear_browser_cache()

            logger.info(
                "Reloading page with clean post-consent capture"
            )

            await page.reload(
                wait_until="load",
                timeout=self.PAGE_LOAD_TIMEOUT_MS,
            )

            if hasattr(self, "security_verification"):
                await self.security_verification.interrupt_if_detected(
                    page,
                    stage="post-consent reload",
                )

            await self._wait_for_page_ready(page)

            if hasattr(self, "security_verification"):
                await self.security_verification.interrupt_if_detected(
                    page,
                    stage="post-consent page readiness",
                )

            await asyncio.sleep(random.uniform(1.0, 2.0))

        await self._open_visible_links(page)
        await self._scroll_to_bottom_and_open_links(page)
        await self._scroll_to_top(page)

        if hasattr(self, "security_verification"):
            await self.security_verification.interrupt_if_detected(
                page,
                stage="after click journey scrolling",
            )

        logger.info(
            "Click journey completed | links_opened=%d | unique_urls=%d",
            self.total_links_opened,
            len(self.clicked_urls),
        )

        await asyncio.sleep(
            random.uniform(
                self.FINAL_WAIT_MIN_SECONDS,
                self.FINAL_WAIT_MAX_SECONDS,
            )
        )

    async def _scroll_to_bottom_and_open_links(
        self,
        page: Page,
    ) -> None:
        deadline = time.monotonic() + self.MAX_SCROLL_SECONDS
        bottom_confirmations = 0

        while time.monotonic() < deadline:
            if self.total_links_opened >= self.MAX_LINKS_PER_RUN:
                logger.info(
                    "Reached maximum link count of %d",
                    self.MAX_LINKS_PER_RUN,
                )
                break

            if hasattr(self, "security_verification"):
                await self.security_verification.interrupt_if_detected(
                    page,
                    stage="click-journey downward scrolling",
                )

            await self._open_visible_links(page)

            state = await self._get_scroll_state(page)

            scroll_y = int(state["scrollY"])
            viewport_height = int(state["viewportHeight"])
            document_height = int(state["documentHeight"])
            at_bottom = bool(state["atBottom"])

            if at_bottom:
                await asyncio.sleep(self.BOTTOM_SETTLE_SECONDS)

                updated_state = await self._get_scroll_state(page)
                updated_height = int(updated_state["documentHeight"])

                if updated_height > document_height:
                    logger.info(
                        "Lazy-loaded content increased page height from %d to %d",
                        document_height,
                        updated_height,
                    )
                    bottom_confirmations = 0
                    continue

                bottom_confirmations += 1

                if bottom_confirmations >= self.MAX_BOTTOM_CONFIRMATIONS:
                    await self._open_visible_links(page)

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

            if hasattr(self, "_smooth_wheel_scroll"):
                await self._smooth_wheel_scroll(page, delta)
            else:
                await page.mouse.wheel(0, delta)

            await asyncio.sleep(
                random.uniform(
                    self.SCROLL_PAUSE_MIN,
                    self.SCROLL_PAUSE_MAX,
                )
            )

        logger.info(
            "Stopped downward click journey | links_opened=%d",
            self.total_links_opened,
        )

    async def _open_visible_links(
        self,
        page: Page,
    ) -> None:
        if self.total_links_opened >= self.MAX_LINKS_PER_RUN:
            return

        candidates = page.locator(
            """
            a[href]:visible,
            [role="link"][href]:visible,
            [data-href]:visible
            """
        )

        try:
            count = await candidates.count()
        except PlaywrightError as exc:
            logger.debug(
                "Unable to enumerate visible links: %s",
                exc,
            )
            return

        opened_in_viewport = 0

        indexes = list(range(count))
        random.shuffle(indexes)

        for index in indexes:
            if (
                opened_in_viewport >= self.MAX_LINKS_PER_VIEWPORT
                or self.total_links_opened >= self.MAX_LINKS_PER_RUN
            ):
                break

            locator = candidates.nth(index)

            candidate = await self._describe_candidate(page, locator)

            if candidate is None:
                continue

            url, text = candidate

            if url in self.clicked_urls:
                continue

            if not self._is_eligible_link(
                url=url,
                text=text,
                original_url=page.url,
            ):
                continue

            opened = await self._open_in_new_tab(
                page=page,
                locator=locator,
                destination_url=url,
                link_text=text,
            )

            if not opened:
                continue

            self.clicked_urls.add(url)
            self.total_links_opened += 1
            opened_in_viewport += 1

            await asyncio.sleep(random.uniform(0.4, 1.1))

    async def _describe_candidate(
        self,
        page: Page,
        locator: Locator,
    ) -> tuple[str, str] | None:
        try:
            if not await locator.is_visible():
                return None

            href = (
                await locator.get_attribute("href")
                or await locator.get_attribute("data-href")
                or ""
            ).strip()

            if not href:
                return None

            text = (
                await locator.inner_text(timeout=1_500)
            ).strip()

            absolute_url = urljoin(page.url, href)

            return absolute_url, text

        except (
            PlaywrightError,
            PlaywrightTimeoutError,
        ):
            return None

    def _is_eligible_link(
        self,
        *,
        url: str,
        text: str,
        original_url: str,
    ) -> bool:
        parsed = urlsplit(url)
        original = urlsplit(original_url)

        if not parsed.scheme or not parsed.netloc:
            return False

        if parsed.scheme.lower() in self.SKIP_SCHEMES:
            return False

        if parsed.scheme.lower() not in {"http", "https"}:
            return False

        normalized_text = " ".join(text.casefold().split())

        if any(
            term in normalized_text
            for term in self.SKIP_TEXT_TERMS
        ):
            return False

        normalized_path = parsed.path.casefold()

        if any(
            term in normalized_path
            for term in self.SKIP_PATH_TERMS
        ):
            return False

        same_document = (
            parsed.scheme == original.scheme
            and parsed.netloc == original.netloc
            and parsed.path.rstrip("/") == original.path.rstrip("/")
            and parsed.query == original.query
        )

        if same_document and parsed.fragment:
            return False

        return True

    async def _open_in_new_tab(
        self,
        *,
        page: Page,
        locator: Locator,
        destination_url: str,
        link_text: str,
    ) -> bool:
        """
        Open the actual link in a new Chrome tab using a macOS Command-click.

        Before clicking:
        - smoothly scroll the element to the center of the viewport
        - wait for the scroll to settle
        - move the visual cursor to the element
        - pulse the visual cursor
        - Command-click the element location

        If the modifier-click does not create a child page reliably, fall back to
        opening a new page directly and navigating it to the discovered URL.

        The original test page is never intentionally navigated away.
        """

        context = page.context
        child_page: Page | None = None

        logger.info(
            "Opening link in new tab "
            "| text=%r "
            "| url=%s",
            link_text[:120],
            destination_url,
        )

        try:
            #
            # Explicitly scroll the link to the center of the viewport.
            #
            await locator.evaluate(
                """
                (element) => {
                    element.scrollIntoView({
                        behavior: "smooth",
                        block: "center",
                        inline: "center"
                    });
                }
                """
            )

            #
            # Give the smooth scroll time to finish before measuring
            # the element and moving the synthetic cursor.
            #
            await asyncio.sleep(0.6)

            #
            # Re-check visibility after scrolling.
            #
            if not await locator.is_visible():
                raise PlaywrightError(
                    "Link is not visible after scrolling into view"
                )

            box = await locator.bounding_box()

            if box is None:
                raise PlaywrightError(
                    "Unable to determine link bounds"
                )

            x = box["x"] + box["width"] / 2
            y = box["y"] + box["height"] / 2

            #
            # Begin waiting for the child tab before clicking.
            #
            async with context.expect_page(
                timeout=3_000,
            ) as page_info:

                #
                # Move the synthetic cursor to the link so the recording
                # visibly shows what KATNIP is about to click.
                #
                if self.visual_cursor is not None:
                    await self.visual_cursor.move_to(
                        page,
                        x,
                        y,
                        steps=24,
                    )

                    await self.visual_cursor.pulse(
                        page,
                    )

                    # Make the click pulse visible in the recording.
                    await asyncio.sleep(0.15)

                #
                # Playwright's Mouse.click() does not accept modifiers,
                # so hold Command down explicitly around the mouse click.
                #
                await page.keyboard.down("Meta")

                try:
                    await page.mouse.click(
                        x,
                        y,
                    )
                finally:
                    await page.keyboard.up("Meta")

            child_page = await page_info.value

        except (
            PlaywrightTimeoutError,
            PlaywrightError,
        ) as exc:
            #
            # Some elements use JavaScript handlers or do not behave like
            # conventional anchors under modifier-click. Opening the discovered
            # URL in a new child page is safer than risking navigation of the
            # original page.
            #
            logger.debug(
                "Modifier-click did not create a tab; "
                "opening destination directly "
                "| url=%s "
                "| reason=%s",
                destination_url,
                exc,
            )

            try:
                child_page = await context.new_page()

                await child_page.goto(
                    destination_url,
                    wait_until="domcontentloaded",
                    timeout=self.CHILD_PAGE_LOAD_TIMEOUT_MS,
                )

            except PlaywrightError as exc:
                logger.warning(
                    "Unable to open child page "
                    "| url=%s "
                    "| error=%s",
                    destination_url,
                    exc,
                )

                if child_page is not None:
                    try:
                        await child_page.close()
                    except PlaywrightError:
                        pass

                await page.bring_to_front()

                return False

        #
        # Wait for the child page to become usable.
        #
        try:
            await child_page.wait_for_load_state(
                "domcontentloaded",
                timeout=self.CHILD_PAGE_LOAD_TIMEOUT_MS,
            )

        except PlaywrightTimeoutError:
            logger.warning(
                "Child page did not reach DOMContentLoaded "
                "| url=%s",
                child_page.url,
            )

        #
        # Preserve the security verification handling you already use.
        #
        if hasattr(
            self,
            "security_verification",
        ):
            await self.security_verification.interrupt_if_detected(
                child_page,
                stage=f"child tab: {destination_url}",
            )

        #
        # Leave the child tab open briefly so its network traffic
        # has time to occur.
        #
        await asyncio.sleep(
            random.uniform(
                self.CHILD_PAGE_WAIT_MIN_SECONDS,
                self.CHILD_PAGE_WAIT_MAX_SECONDS,
            )
        )

        logger.info(
            "Closing child tab "
            "| final_url=%s "
            "| title=%r",
            child_page.url,
            await self._safe_title(
                child_page
            ),
        )

        try:
            await child_page.close()
        except PlaywrightError as exc:
            logger.debug(
                "Unable to close child tab cleanly: %s",
                exc,
            )

        #
        # Return focus to the original target page.
        #
        await page.bring_to_front()

        #
        # Reset the visual cursor position after returning to the parent page.
        # This avoids stale coordinates after tab switching.
        #
        if self.visual_cursor is not None:
            self.visual_cursor.x = None
            self.visual_cursor.y = None

        return True

    async def _safe_title(
        self,
        page: Page,
    ) -> str:
        try:
            return await page.title()
        except PlaywrightError:
            return ""
