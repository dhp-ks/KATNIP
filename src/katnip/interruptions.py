from __future__ import annotations

import logging
from dataclasses import dataclass

from playwright.async_api import Page


logger = logging.getLogger("katnip.interruptions")


class JourneyInterruptedError(RuntimeError):
    """Raised when a page condition requires the journey to stop."""


@dataclass(frozen=True)
class InterruptionResult:
    detected: bool
    provider: str | None = None
    reason: str | None = None
    page_title: str | None = None
    page_url: str | None = None


class SecurityVerificationHandler:
    """
    Detects security-verification and anti-bot interstitial pages.

    This handler intentionally does not attempt to solve, bypass, or interact
    with the verification challenge. It interrupts the journey so that KATNIP
    can retain the recording, mitmproxy output, and logs.
    """

    CLOUDFLARE_TITLE_PATTERNS = (
        "just a moment",
        "attention required",
        "performing security verification",
        "security verification",
    )

    CLOUDFLARE_BODY_PATTERNS = (
        "performing security verification",
        "verifies you are not a bot",
        "verify you are human",
        "checking your browser",
        "checking if the site connection is secure",
        "this website uses a security service",
        "enable javascript and cookies to continue",
        "cloudflare ray id",
    )

    CLOUDFLARE_SELECTORS = (
        "#challenge-running",
        "#challenge-stage",
        "#cf-challenge-running",
        ".cf-browser-verification",
        "[data-testid='challenge-stage']",
        "iframe[src*='challenges.cloudflare.com']",
        "script[src*='challenges.cloudflare.com']",
    )

    async def detect(self, page: Page) -> InterruptionResult:
        title = await self._safe_title(page)
        url = page.url

        normalized_title = title.casefold()

        for pattern in self.CLOUDFLARE_TITLE_PATTERNS:
            if pattern in normalized_title:
                return InterruptionResult(
                    detected=True,
                    provider="cloudflare",
                    reason=f"Page title contained {pattern!r}",
                    page_title=title,
                    page_url=url,
                )

        for selector in self.CLOUDFLARE_SELECTORS:
            try:
                locator = page.locator(selector).first

                if await locator.count() > 0:
                    return InterruptionResult(
                        detected=True,
                        provider="cloudflare",
                        reason=f"Challenge element matched {selector!r}",
                        page_title=title,
                        page_url=url,
                    )
            except Exception:
                logger.debug(
                    "Could not evaluate security-verification selector %r",
                    selector,
                    exc_info=True,
                )

        body_text = await self._safe_body_text(page)
        normalized_body = body_text.casefold()

        for pattern in self.CLOUDFLARE_BODY_PATTERNS:
            if pattern in normalized_body:
                return InterruptionResult(
                    detected=True,
                    provider="cloudflare",
                    reason=f"Page text contained {pattern!r}",
                    page_title=title,
                    page_url=url,
                )

        return InterruptionResult(
            detected=False,
            page_title=title,
            page_url=url,
        )

    async def interrupt_if_detected(
        self,
        page: Page,
        *,
        stage: str,
    ) -> None:
        result = await self.detect(page)

        if not result.detected:
            return

        message = (
            "Journey interrupted by security-verification page "
            f"| provider={result.provider} "
            f"| stage={stage} "
            f"| reason={result.reason} "
            f"| title={result.page_title!r} "
            f"| url={result.page_url!r}"
        )

        logger.error(message)
        raise JourneyInterruptedError(message)

    async def _safe_title(self, page: Page) -> str:
        try:
            return await page.title()
        except Exception:
            logger.debug(
                "Could not read page title while checking for interruption",
                exc_info=True,
            )
            return ""

    async def _safe_body_text(self, page: Page) -> str:
        try:
            return await page.locator("body").inner_text(timeout=2_000)
        except Exception:
            logger.debug(
                "Could not read page body while checking for interruption",
                exc_info=True,
            )
            return ""