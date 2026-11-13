from __future__ import annotations

import json
import random
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def safe_slug(value: str, max_length: int = 60) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return (value or "run")[:max_length]


def random_between(minimum: float, maximum: float) -> float:
    return random.uniform(minimum, maximum)


def strip_url_query(url: str) -> str:
    try:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except ValueError:
        return url


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
