from __future__ import annotations

import asyncio

from playwright.async_api import Locator, Page


CURSOR_INSTALL_SCRIPT = """
(() => {
    function createCursor() {
        if (document.getElementById("katnip-visual-cursor")) {
            return;
        }

        const cursor = document.createElement("div");

        cursor.id = "katnip-visual-cursor";

        Object.assign(cursor.style, {
            position: "fixed",
            left: "0px",
            top: "0px",
            width: "18px",
            height: "18px",
            borderRadius: "50%",
            background: "#5856ff",
            border: "2px solid white",
            boxShadow: "0 0 4px rgba(0,0,0,0.75)",
            pointerEvents: "none",
            zIndex: "2147483647",
            transform: "translate(-50%, -50%)"
        });

        document.documentElement.appendChild(cursor);
    }

    if (document.documentElement) {
        createCursor();
    } else {
        document.addEventListener(
            "DOMContentLoaded",
            createCursor,
            { once: true }
        );
    }
})();
"""


class VisualCursor:
    def __init__(
        self,
        step_delay_seconds: float = 0.012,
    ) -> None:
        self.step_delay_seconds = step_delay_seconds
        self.x: float | None = None
        self.y: float | None = None

    async def install(
        self,
        page: Page,
    ) -> None:
        await page.evaluate(
            CURSOR_INSTALL_SCRIPT
        )

    async def move_to(
        self,
        page: Page,
        x: float,
        y: float,
        *,
        steps: int = 20,
    ) -> None:
        await self.install(page)

        if self.x is None or self.y is None:
            self.x = x
            self.y = y

            await page.mouse.move(
                x,
                y,
            )

            await self._position(
                page,
                x,
                y,
            )

            return

        start_x = self.x
        start_y = self.y

        for step in range(
            1,
            steps + 1,
        ):
            progress = step / steps

            current_x = (
                start_x
                + (x - start_x) * progress
            )

            current_y = (
                start_y
                + (y - start_y) * progress
            )

            await page.mouse.move(
                current_x,
                current_y,
            )

            await self._position(
                page,
                current_x,
                current_y,
            )

            await asyncio.sleep(
                self.step_delay_seconds
            )

        self.x = x
        self.y = y

    async def click_locator(
        self,
        page: Page,
        locator: Locator,
        *,
        timeout_ms: int,
    ) -> bool:
        await locator.wait_for(
            state="visible",
            timeout=timeout_ms,
        )

        await locator.scroll_into_view_if_needed()

        box = await locator.bounding_box()

        if box is None:
            return False

        x = (
            box["x"]
            + box["width"] / 2
        )

        y = (
            box["y"]
            + box["height"] / 2
        )

        await self.move_to(
            page,
            x,
            y,
            steps=24,
        )

        await self.pulse(
            page
        )

        await page.mouse.click(
            x,
            y,
        )

        return True

    async def _position(
        self,
        page: Page,
        x: float,
        y: float,
    ) -> None:
        await page.evaluate(
            """
            ([x, y]) => {
                const cursor =
                    document.getElementById(
                        "katnip-visual-cursor"
                    );

                if (!cursor) {
                    return;
                }

                cursor.style.left = `${x}px`;
                cursor.style.top = `${y}px`;
            }
            """,
            [x, y],
        )

    async def pulse(
        self,
        page: Page,
    ) -> None:
        await page.evaluate(
            """
            () => {
                const cursor =
                    document.getElementById(
                        "katnip-visual-cursor"
                    );

                if (!cursor) {
                    return;
                }

                cursor.animate(
                    [
                        {
                            width: "18px",
                            height: "18px"
                        },
                        {
                            width: "32px",
                            height: "32px"
                        },
                        {
                            width: "18px",
                            height: "18px"
                        }
                    ],
                    {
                        duration: 220,
                        easing: "ease-out"
                    }
                );
            }
            """
        )