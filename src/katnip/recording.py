from __future__ import annotations

import logging
import time
from pathlib import Path

from Quartz import (
    CGWindowListCopyWindowInfo,
    kCGNullWindowID,
    kCGWindowListExcludeDesktopElements,
    kCGWindowListOptionOnScreenOnly,
)

from katnip.config import RecordingConfig
from katnip.process import ManagedProcess


class RecordingManager:
    def __init__(self, config: RecordingConfig, run_dir: Path, window_marker: str) -> None:
        self.config = config
        self.run_dir = run_dir
        self.output_dir = run_dir / "recording"
        self.output_path = self.output_dir / "browser.mp4"
        self.log_path = run_dir / "logs" / "recording.log"
        self.process: ManagedProcess | None = None
        self.logger = logging.getLogger("katnip.recording")
        self.window_marker = window_marker

    def _find_chrome_window_id(self) -> int:
        for attempt in range(1, 41):
            windows = CGWindowListCopyWindowInfo(
                kCGWindowListOptionOnScreenOnly
                | kCGWindowListExcludeDesktopElements,
                kCGNullWindowID,
            )

            matches: list[dict[str, object]] = []

            self.logger.info(
                "Quartz lookup attempt %d/40; total windows=%d",
                attempt,
                len(windows),
            )

            for window in windows:
                owner = str(window.get("kCGWindowOwnerName", ""))
                title = str(window.get("kCGWindowName", ""))
                window_id = window.get("kCGWindowNumber")
                pid = window.get("kCGWindowOwnerPID")
                layer = window.get("kCGWindowLayer")
                onscreen = window.get("kCGWindowIsOnscreen")

                if "chrome" not in owner.lower():
                    continue

                self.logger.info(
                    "Chrome window "
                    "| owner=%r "
                    "| pid=%r "
                    "| window_id=%r "
                    "| title=%r "
                    "| layer=%r "
                    "| onscreen=%r",
                    owner,
                    pid,
                    window_id,
                    title,
                    layer,
                    onscreen,
                )

                if (
                    self.window_marker in title
                    and isinstance(window_id, int)
                ):
                    matches.append(
                        {
                            "owner": owner,
                            "title": title,
                            "pid": pid,
                            "window_id": window_id,
                        }
                    )

            if len(matches) == 1:
                match = matches[0]
                window_id = int(match["window_id"])

                self.logger.info(
                    "Selected KATNIP Chrome window "
                    "| window_id=%d "
                    "| pid=%r "
                    "| title=%r",
                    window_id,
                    match["pid"],
                    match["title"],
                )

                return window_id

            if len(matches) > 1:
                raise RuntimeError(
                    "Found multiple Chrome windows containing "
                    f"{self.window_marker!r}: {matches!r}"
                )

            time.sleep(0.25)

        raise RuntimeError(
            "Could not find the Chrome window containing "
            f"{self.window_marker!r}"
        )

    def build_command(self) -> list[str]:
        window_id = self._find_chrome_window_id()

        command = [
            self.config.executable,
            "--window-id",
            str(window_id),
            "--output",
            str(self.output_path),
            "--fps",
            str(self.config.fps),
        ]

        if self.config.include_cursor:
            command.append("--include-cursor")

        return command

    def start(self) -> None:
        if not self.config.enabled:
            self.logger.info("Screen recording disabled by configuration")
            return

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        self.process = ManagedProcess(
            name="recording",
            command=self.build_command(),
            log_path=self.log_path,
            cwd=self.run_dir,
        )

        self.process.start()
        self.process.ensure_running()

        self.logger.info(
            "Recording selected Chrome window to %s",
            self.output_path,
        )

    def stop(self) -> None:
        if self.process is not None:
            self.process.stop(timeout_seconds=20)