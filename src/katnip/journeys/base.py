from __future__ import annotations

from abc import ABC, abstractmethod

from playwright.async_api import Page


class Journey(ABC):
    name: str

    @abstractmethod
    async def run(self, page: Page, website: str) -> None:
        raise NotImplementedError
