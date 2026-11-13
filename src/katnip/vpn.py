from __future__ import annotations

import time
import logging
import subprocess
from pathlib import Path
from urllib.request import Request, urlopen

from katnip.config import VPNConfig


class VPNError(RuntimeError):
    pass


class VPNManager:
    def __init__(self, config: VPNConfig, location: str, log_path: Path) -> None:
        self.config = config
        self.location = location
        self.log_path = log_path
        self.connected = False
        self.logger = logging.getLogger("katnip.vpn")

    def _render_command(self, template: list[str]) -> list[str]:
        location_config = self.config.locations[self.location]
        values = {
            "config_path": str(location_config.config_path),
            "location": self.location,
        }
        return [part.format(**values) for part in template]

    def _run(self, command: list[str], action: str) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.logger.info("VPN %s: %s", action, " ".join(command))
        with self.log_path.open("ab") as log_handle:
            result = subprocess.run(
                command,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                timeout=self.config.connect_timeout_seconds,
                check=False,
            )
        if result.returncode != 0:
            raise VPNError(
                f"VPN {action} command failed with code {result.returncode}; "
                f"see {self.log_path}"
            )

    def public_ip(self) -> str | None:
        if not self.config.verify_public_ip:
            return None

        max_attempts = 4
        base_delay_seconds = 1.0
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                request = Request(
                    self.config.public_ip_check_url,
                    headers={"User-Agent": "KATNIP/0.1"},
                )

                with urlopen(
                    request,
                    timeout=self.config.public_ip_timeout_seconds,
                ) as response:
                    public_ip = (
                        response.read()
                        .decode("utf-8", errors="replace")
                        .strip()
                    )

                if public_ip:
                    if attempt > 1:
                        self.logger.info(
                            "Public IP lookup succeeded on attempt %d/%d",
                            attempt,
                            max_attempts,
                        )

                    return public_ip

                last_error = RuntimeError(
                    "Public IP service returned an empty response"
                )

            except Exception as exc:
                last_error = exc

            if attempt < max_attempts:
                delay = base_delay_seconds * attempt

                self.logger.warning(
                    "Public IP lookup attempt %d/%d failed: %s; "
                    "retrying in %.1f seconds",
                    attempt,
                    max_attempts,
                    last_error,
                    delay,
                )

                time.sleep(delay)

        self.logger.warning(
            "Unable to determine public IP after %d attempts: %s",
            max_attempts,
            last_error,
        )

        return None

    def connect(self) -> None:
        if not self.config.enabled:
            self.logger.info("VPN disabled by configuration")
            return
        self._run(self._render_command(self.config.up_command), "connect")
        self.connected = True

    def disconnect(self) -> None:
        if not self.config.enabled or not self.connected:
            return
        try:
            self._run(self._render_command(self.config.down_command), "disconnect")
        finally:
            self.connected = False
