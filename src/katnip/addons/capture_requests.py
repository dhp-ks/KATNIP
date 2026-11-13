"""Bundled mitmproxy addon that writes one privacy-conscious JSON record per request."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from mitmproxy import ctx, http


class KATNIPRequestCapture:
    def __init__(self) -> None:
        self.handle: TextIO | None = None

    def load(self, loader) -> None:
        loader.add_option(
            name="katnip_output_dir",
            typespec=str,
            default=".",
            help="Directory where KATNIP request records are written.",
        )

    def running(self) -> None:
        output_dir = Path(ctx.options.katnip_output_dir).expanduser()
        output_dir.mkdir(parents=True, exist_ok=True)
        self.handle = (output_dir / "capture_requests.jsonl").open(
            "a", encoding="utf-8", buffering=1
        )

    def request(self, flow: http.HTTPFlow) -> None:
        if self.handle is None:
            return
        request = flow.request
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "method": request.method,
            "scheme": request.scheme,
            "host": request.pretty_host,
            "port": request.port,
            "path": request.path,
            "url": request.pretty_url,
            "http_version": request.http_version,
        }
        self.handle.write(json.dumps(record, sort_keys=True) + "\n")

    def done(self) -> None:
        if self.handle is not None:
            self.handle.flush()
            self.handle.close()
            self.handle = None


addons = [KATNIPRequestCapture()]
