from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml


logger = logging.getLogger("katnip.technology")


@dataclass(frozen=True)
class TechnologyMatch:
    technology_id: str
    name: str
    company: str
    category: str
    description: str
    matched_by: str
    confidence: str


@dataclass(frozen=True)
class TechnologyRule:
    technology_id: str
    name: str
    company: str
    category: str
    description: str
    priority: int = 0

    hosts: frozenset[str] = field(default_factory=frozenset)
    domains: frozenset[str] = field(default_factory=frozenset)
    host_suffixes: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    path_prefixes: tuple[str, ...] = ()
    path_suffixes: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TechnologyRule:
        match = value.get("match") or {}

        return cls(
            technology_id=str(value["id"]),
            name=str(value["name"]),
            company=str(value.get("company") or ""),
            category=str(value.get("category") or "unknown"),
            description=str(value.get("description") or ""),
            priority=int(value.get("priority") or 0),
            hosts=frozenset(
                normalize_host(item)
                for item in match.get("hosts", [])
            ),
            domains=frozenset(
                normalize_host(item)
                for item in match.get("domains", [])
            ),
            host_suffixes=tuple(
                normalize_suffix(item)
                for item in match.get("host_suffixes", [])
            ),
            paths=tuple(
                normalize_path(item)
                for item in match.get("paths", [])
            ),
            path_prefixes=tuple(
                normalize_path(item)
                for item in match.get("path_prefixes", [])
            ),
            path_suffixes=tuple(
                normalize_path(item)
                for item in match.get("path_suffixes", [])
            ),
        )

    def match(
        self,
        *,
        host: str,
        registrable_domain: str,
        path: str,
    ) -> tuple[int, str, str] | None:
        """
        Return:

            score, matched_by, confidence

        Higher scores represent more specific matches.
        """

        scores: list[tuple[int, str, str]] = []

        host_matches = host in self.hosts
        domain_matches = registrable_domain in self.domains

        suffix_matches = any(
            host == suffix.removeprefix(".")
            or host.endswith(suffix)
            for suffix in self.host_suffixes
        )

        exact_path_matches = any(
            path == candidate
            or path.rstrip("/") == candidate.rstrip("/")
            for candidate in self.paths
        )

        prefix_matches = any(
            path.startswith(prefix)
            for prefix in self.path_prefixes
        )

        suffix_path_matches = any(
            path.endswith(suffix)
            for suffix in self.path_suffixes
        )

        any_path_match = (
            exact_path_matches
            or prefix_matches
            or suffix_path_matches
        )

        if host_matches and any_path_match:
            scores.append(
                (
                    1_000 + self.priority,
                    "exact host and path",
                    "high",
                )
            )

        if suffix_matches and any_path_match:
            scores.append(
                (
                    950 + self.priority,
                    "host suffix and path",
                    "high",
                )
            )

        if domain_matches and any_path_match:
            scores.append(
                (
                    900 + self.priority,
                    "registrable domain and path",
                    "high",
                )
            )

        if host_matches:
            scores.append(
                (
                    700 + self.priority,
                    "exact host",
                    "high",
                )
            )

        if suffix_matches:
            scores.append(
                (
                    600 + self.priority,
                    "host suffix",
                    "medium",
                )
            )

        if domain_matches:
            scores.append(
                (
                    500 + self.priority,
                    "registrable domain",
                    "medium",
                )
            )

        # Path-only matching is intentionally weaker because common paths
        # such as /collect can occur on unrelated first-party endpoints.
        if exact_path_matches:
            scores.append(
                (
                    200 + self.priority,
                    "path only",
                    "low",
                )
            )

        if suffix_path_matches:
            scores.append(
                (
                    175 + self.priority,
                    "path suffix only",
                    "low",
                )
            )

        if prefix_matches:
            scores.append(
                (
                    150 + self.priority,
                    "path prefix only",
                    "low",
                )
            )

        if not scores:
            return None

        return max(scores, key=lambda item: item[0])


class TechnologyCatalog:
    def __init__(self, rules: list[TechnologyRule]) -> None:
        self.rules = sorted(
            rules,
            key=lambda rule: rule.priority,
            reverse=True,
        )

    @classmethod
    def from_yaml(cls, path: Path) -> TechnologyCatalog:
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}

        technologies = data.get("technologies") or []

        if not isinstance(technologies, list):
            raise ValueError(
                f"technology catalog must contain a technologies list: {path}"
            )

        rules = [
            TechnologyRule.from_dict(item)
            for item in technologies
        ]

        logger.info(
            "Loaded %d technology-identification rules from %s",
            len(rules),
            path,
        )

        return cls(rules)

    def identify(
        self,
        *,
        host: str,
        registrable_domain: str,
        url: str | None = None,
        path: str | None = None,
    ) -> TechnologyMatch | None:
        normalized_host = normalize_host(host)
        normalized_domain = normalize_host(registrable_domain)

        if path is None:
            path = urlsplit(url or "").path

        normalized_path = normalize_path(path)

        best: tuple[int, TechnologyRule, str, str] | None = None

        for rule in self.rules:
            result = rule.match(
                host=normalized_host,
                registrable_domain=normalized_domain,
                path=normalized_path,
            )

            if result is None:
                continue

            score, matched_by, confidence = result

            if best is None or score > best[0]:
                best = (
                    score,
                    rule,
                    matched_by,
                    confidence,
                )

        if best is None:
            return None

        _, rule, matched_by, confidence = best

        return TechnologyMatch(
            technology_id=rule.technology_id,
            name=rule.name,
            company=rule.company,
            category=rule.category,
            description=rule.description,
            matched_by=matched_by,
            confidence=confidence,
        )


def normalize_host(value: str) -> str:
    return value.strip().lower().rstrip(".")


def normalize_suffix(value: str) -> str:
    normalized = normalize_host(value)

    if not normalized.startswith("."):
        normalized = f".{normalized}"

    return normalized


def normalize_path(value: str) -> str:
    normalized = value.strip()

    if not normalized:
        return "/"

    if not normalized.startswith("/"):
        normalized = f"/{normalized}"

    return normalized


@lru_cache(maxsize=1)
def default_catalog() -> TechnologyCatalog:
    catalog_resource = (
        files("katnip")
        .joinpath("data")
        .joinpath("technology_catalog.yaml")
    )

    with catalog_resource.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    rules = [
        TechnologyRule.from_dict(item)
        for item in data.get("technologies", [])
    ]

    return TechnologyCatalog(rules)