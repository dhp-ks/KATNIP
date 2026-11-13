from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

from playwright.async_api import (
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from katnip.config import ConsentConfig
from katnip.visual_cursor import VisualCursor


logger = logging.getLogger("katnip.consent")

ConsentAction = Literal["accept", "reject"]


@dataclass(frozen=True)
class ConsentResult:
    handled: bool
    provider: str | None = None
    action: ConsentAction | None = None
    selector: str | None = None



async def click_if_visible(
    locator: Locator,
    *,
    timeout_ms: int,
    page: Page,
    visual_cursor: VisualCursor | None = None,
) -> bool:
    try:
        await locator.wait_for(
            state="visible",
            timeout=timeout_ms,
        )

        await locator.scroll_into_view_if_needed()

        if visual_cursor is not None:
            return await visual_cursor.click_locator(
                page,
                locator,
                timeout_ms=timeout_ms,
            )

        await locator.click(
            timeout=timeout_ms,
        )

        return True

    except PlaywrightTimeoutError:
        return False

    except Exception as exc:
        logger.debug(
            "Consent control could not be clicked: %s",
            exc,
        )
        return False


class ConsentProvider:
    name = "unknown"

    def __init__(
        self,
        visual_cursor: VisualCursor | None = None,
    ) -> None:
        self.visual_cursor = visual_cursor
        
    async def handle(
        self,
        page: Page,
        action: ConsentAction,
        timeout_ms: int,
    ) -> ConsentResult:
        raise NotImplementedError


class OneTrustConsentProvider(ConsentProvider):
    """
    OneTrust-specific matching.

    This provider first tries direct OneTrust accept/reject controls.

    For reject flows where no direct "Reject All" button is available,
    it opens the OneTrust preference center, disables all enabled
    optional category switches, and clicks "Confirm My Choices".
    """

    name = "onetrust"

    ACCEPT_SELECTORS = (
        "#onetrust-accept-btn-handler",
        "button#onetrust-accept-btn-handler",
    )

    REJECT_SELECTORS = (
        "#onetrust-reject-all-handler",
        "button#onetrust-reject-all-handler",
        "#ot-pc-refuse-all-handler",
    )

    PREFERENCE_CENTER_SELECTORS = (
        "#onetrust-pc-btn-handler",
        "button#onetrust-pc-btn-handler",
    )

    SWITCH_CHECKBOX_SELECTOR = (
        ".ot-tgl "
        "input[type='checkbox'].category-switch-handler"
    )

    CONFIRM_CHOICES_SELECTORS = (
        "button.save-preference-btn-handler.onetrust-close-btn-handler",
        ".save-preference-btn-handler.onetrust-close-btn-handler",
    )

    async def handle(
        self,
        page: Page,
        action: ConsentAction,
        timeout_ms: int,
    ) -> ConsentResult:
        if action == "accept":
            return await self._try_selectors(
                page,
                action,
                self.ACCEPT_SELECTORS,
                timeout_ms,
            )

        #
        # REJECT FLOW
        #

        # First try a direct OneTrust reject-all button.
        result = await self._try_selectors(
            page,
            action,
            self.REJECT_SELECTORS,
            timeout_ms,
        )

        if result.handled:
            return result

        # Some OneTrust implementations require opening the
        # preference center before reject controls become available.
        opened_selector = await self._open_preference_center(
            page,
            timeout_ms,
        )

        if opened_selector is None:
            return ConsentResult(
                handled=False,
            )

        logger.info(
            "OneTrust preference center opened"
        )

        # Give the preference center time to finish rendering.
        await asyncio.sleep(0.5)

        # Try a direct reject-all button again now that the
        # preference center is visible.
        result = await self._try_selectors(
            page,
            action,
            self.REJECT_SELECTORS,
            timeout_ms,
        )

        if result.handled:
            return result

        # Final fallback:
        # turn off every enabled OneTrust category and save.
        return await self._disable_all_and_confirm(
            page,
            action,
            timeout_ms,
        )

    async def _try_selectors(
        self,
        page: Page,
        action: ConsentAction,
        selectors: tuple[str, ...],
        timeout_ms: int,
    ) -> ConsentResult:
        deadline = (
            asyncio.get_running_loop().time()
            + timeout_ms / 1000
        )

        while asyncio.get_running_loop().time() < deadline:
            for selector in selectors:
                locator = page.locator(
                    selector
                ).first

                try:
                    if not await locator.is_visible():
                        continue

                    if await click_if_visible(
                        locator,
                        timeout_ms=1000,
                        page=page,
                        visual_cursor=self.visual_cursor,
                    ):
                        logger.info(
                            "Clicked OneTrust consent control "
                            "| action=%s "
                            "| selector=%s",
                            action,
                            selector,
                        )

                        return ConsentResult(
                            handled=True,
                            provider=self.name,
                            action=action,
                            selector=selector,
                        )

                except Exception:
                    continue

            await asyncio.sleep(0.1)

        return ConsentResult(
            handled=False,
        )

    async def _open_preference_center(
        self,
        page: Page,
        timeout_ms: int,
    ) -> str | None:
        for selector in self.PREFERENCE_CENTER_SELECTORS:
            locator = page.locator(
                selector
            ).first

            if await click_if_visible(
                locator,
                timeout_ms=timeout_ms,
                page=page,
                visual_cursor=self.visual_cursor,
            ):
                logger.info(
                    "Opened OneTrust preference center "
                    "| selector=%s",
                    selector,
                )

                return selector

        return None

    async def _disable_all_and_confirm(
        self,
        page: Page,
        action: ConsentAction,
        timeout_ms: int,
    ) -> ConsentResult:
        checkboxes = page.locator(
            self.SWITCH_CHECKBOX_SELECTOR
        )

        count = await checkboxes.count()

        logger.info(
            "OneTrust preference center "
            "| switch_count=%d",
            count,
        )

        if count == 0:
            logger.warning(
                "OneTrust preference center opened, "
                "but no category switches were found"
            )

        disabled_count = 0
        already_disabled_count = 0
        unavailable_count = 0

        for index in range(count):
            checkbox = checkboxes.nth(
                index
            )

            try:
                checkbox_id = (
                    await checkbox.get_attribute(
                        "id"
                    )
                )

                group_id = (
                    await checkbox.get_attribute(
                        "data-optanongroupid"
                    )
                )

                label_text = ""

                label = None

                if checkbox_id:
                    label = page.locator(
                        f"label[for='{checkbox_id}']"
                    ).first

                    if await label.count() > 0:
                        try:
                            label_text = (
                                await label.inner_text()
                            ).strip()
                        except Exception:
                            label_text = ""

                is_checked = (
                    await checkbox.is_checked()
                )

                logger.info(
                    "OneTrust preference "
                    "| index=%d/%d "
                    "| group=%s "
                    "| label=%r "
                    "| enabled=%s",
                    index + 1,
                    count,
                    group_id,
                    label_text,
                    is_checked,
                )

                # Already off -- leave it alone.
                if not is_checked:
                    already_disabled_count += 1
                    continue

                #
                # Click the associated label rather than altering the
                # DOM checkbox state directly. This allows OneTrust's
                # own JavaScript/change handlers to run normally.
                #
                if (
                    label is not None
                    and await label.count() > 0
                ):
                    await label.scroll_into_view_if_needed()

                    await click_if_visible(
                        label,
                        timeout_ms=timeout_ms,
                        page=page,
                        visual_cursor=self.visual_cursor,
                    )

                else:
                    # Fallback if the associated label cannot
                    # be located.
                    await click_if_visible(
                        checkbox,
                        timeout_ms=timeout_ms,
                        page=page,
                        visual_cursor=self.visual_cursor,
                    )

                # Give OneTrust a moment to update its state.
                await asyncio.sleep(0.15)

                # Verify that the preference actually changed.
                still_checked = (
                    await checkbox.is_checked()
                )

                if still_checked:
                    logger.warning(
                        "OneTrust preference did not disable "
                        "| group=%s "
                        "| label=%r",
                        group_id,
                        label_text,
                    )

                    unavailable_count += 1
                    continue

                logger.info(
                    "Disabled OneTrust preference "
                    "| group=%s "
                    "| label=%r",
                    group_id,
                    label_text,
                )

                disabled_count += 1

            except PlaywrightTimeoutError:
                logger.warning(
                    "Timed out handling OneTrust preference "
                    "| index=%d/%d",
                    index + 1,
                    count,
                )

                unavailable_count += 1

            except Exception as exc:
                logger.warning(
                    "Unable to handle OneTrust preference "
                    "| index=%d/%d "
                    "| error=%s",
                    index + 1,
                    count,
                    exc,
                )

                unavailable_count += 1

        logger.info(
            "OneTrust preferences processed "
            "| disabled=%d "
            "| already_disabled=%d "
            "| unavailable=%d",
            disabled_count,
            already_disabled_count,
            unavailable_count,
        )

        #
        # Confirm/save the choices.
        #
        for selector in self.CONFIRM_CHOICES_SELECTORS:
            locator = page.locator(
                selector
            ).first

            if await click_if_visible(
                locator,
                timeout_ms=timeout_ms,
                page=page,
                visual_cursor=self.visual_cursor,
            ):
                logger.info(
                    "Confirmed OneTrust choices "
                    "| selector=%s",
                    selector,
                )

                return ConsentResult(
                    handled=True,
                    provider=self.name,
                    action=action,
                    selector=selector,
                )

        logger.warning(
            "OneTrust preferences were processed, "
            "but Confirm My Choices could not be clicked"
        )

        return ConsentResult(
            handled=False,
        )
        
class CookiebotConsentProvider(ConsentProvider):
    """Cookiebot-specific matching for allow-all and deny-all controls."""

    name = "cookiebot"

    ACCEPT_SELECTORS = (
        "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    )

    REJECT_SELECTORS = (
        "#CybotCookiebotDialogBodyButtonDecline",
    )

    async def handle(
        self,
        page: Page,
        action: ConsentAction,
        timeout_ms: int,
    ) -> ConsentResult:
        selectors = (
            self.ACCEPT_SELECTORS
            if action == "accept"
            else self.REJECT_SELECTORS
        )

        for selector in selectors:
            locator = page.locator(selector).first

            if await click_if_visible(
                locator,
                timeout_ms=timeout_ms,
                page=page,
                visual_cursor=self.visual_cursor,
            ):
                return ConsentResult(
                    handled=True,
                    provider=self.name,
                    action=action,
                    selector=selector,
                )

        return ConsentResult(handled=False)

class GenericConsentProvider(ConsentProvider):
    """
    Generic consent matching based on visible control text.

    This should remain provider-neutral. It can work with OneTrust,
    TrustArc, custom banners, and other CMP implementations when they
    expose conventional button wording.
    """

    name = "generic"

    ACCEPT_TEXTS = (
        "Accept",
        "Accept All",
        "Accept All Cookies",
        "Allow All",
        "Allow All Cookies",
        "Agree",
        "I Agree",
        "I Understand",
        "Got it"
    )

    REJECT_TEXTS = (
        "Reject",
        "Reject All",
        "Reject All Cookies",
        "Opt-Out",
        "Opt Out",
        "Decline All",
        "Decline All Cookies",
        "Deny All",
        "Do Not Sell My Personal Information",
        "Do Not Sell or Share My Personal Information",
        "Do Not Sell or Share",
        "Decline",
        "Deny"
    )

    CONTROL_SELECTORS = (
        "button",
        "[role='button']",
        "input[type='button']",
        "input[type='submit']",
        "a",
    )

    async def handle(
        self,
        page: Page,
        action: ConsentAction,
        timeout_ms: int,
    ) -> ConsentResult:
        texts = (
            self.ACCEPT_TEXTS
            if action == "accept"
            else self.REJECT_TEXTS
        )

        # Try exact accessible-name matches first. These are less likely
        # to click an unrelated control.
        for text in texts:
            locator = page.get_by_role(
                "button",
                name=text,
                exact=True,
            ).first

            if await click_if_visible(
                locator,
                timeout_ms=timeout_ms,
                page=page,
                visual_cursor=self.visual_cursor,
            ):
                return ConsentResult(
                    handled=True,
                    provider=self.name,
                    action=action,
                    selector=f"role=button name={text!r}",
                )

        # Fall back to exact text within common clickable elements.
        for text in texts:
            for control_selector in self.CONTROL_SELECTORS:
                selector = (
                    f"{control_selector}:text-is({text!r})"
                )

                locator = page.locator(selector).first

                if await click_if_visible(
                    locator,
                    timeout_ms=timeout_ms,
                    page=page,
                    visual_cursor=self.visual_cursor,
                ):
                    return ConsentResult(
                        handled=True,
                        provider=self.name,
                        action=action,
                        selector=selector,
                    )

        return ConsentResult(handled=False)


class ConsentManager:

    def __init__(
        self,
        config: ConsentConfig,
        visual_cursor: VisualCursor | None = None,
    ) -> None:
        self.config = config

        self.providers: tuple[
            ConsentProvider,
            ...
        ] = (
            OneTrustConsentProvider(
                visual_cursor=visual_cursor
            ),
            CookiebotConsentProvider(visual_cursor=visual_cursor),
            GenericConsentProvider(
                visual_cursor=visual_cursor
            ),
        )

    async def handle(
        self,
        page: Page,
    ) -> ConsentResult:
        action = self.config.action

        if action == "none":
            logger.info("Consent action disabled")
            return ConsentResult(handled=False)

        timeout_ms = int(
            self.config.timeout_seconds * 1000
        )

        logger.info(
            "Attempting consent action: %s",
            action,
        )

        for provider in self.providers:
            logger.info(
                "Checking consent provider: %s",
                provider.name,
            )

            result = await provider.handle(
                page,
                action,
                timeout_ms,
            )

            if not result.handled:
                continue

            logger.info(
                "Consent action completed "
                "| provider=%s "
                "| action=%s "
                "| selector=%s",
                result.provider,
                result.action,
                result.selector,
            )

            if (
                self.config.post_action_wait_seconds > 0
            ):
                await asyncio.sleep(
                    self.config.post_action_wait_seconds
                )


            return result

        logger.warning(
            "No supported consent banner was found "
            "for action %r",
            action,
        )

        return ConsentResult(handled=False)