from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import IO


class ProcessStartError(RuntimeError):
    pass


class ManagedProcess:
    def __init__(
        self,
        name: str,
        command: list[str],
        log_path: Path,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self.command = command
        self.log_path = log_path
        self.cwd = cwd
        self.env = env
        self.process: subprocess.Popen[bytes] | None = None
        self._log_handle: IO[bytes] | None = None
        self.logger = logging.getLogger(f"katnip.process.{name}")

    def start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = self.log_path.open("ab")
        self.logger.debug("Starting command: %s", self.command)
        try:
            self.process = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                env=self.env,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            self._close_log()
            raise ProcessStartError(f"Unable to start {self.name}: {exc}") from exc

    def ensure_running(self) -> None:
        if self.process is None:
            raise ProcessStartError(f"{self.name} was not started")
        code = self.process.poll()
        if code is not None:
            raise ProcessStartError(
                f"{self.name} exited during startup with code {code}; see {self.log_path}"
            )

    def stop(self, timeout_seconds: float = 20.0) -> None:
        process = self.process

        if process is None:
            self._close_log()
            return

        if process.poll() is not None:
            self._close_log()
            return

        self.logger.debug(
            "Stopping process %s with SIGTERM (pid=%s)",
            self.name,
            process.pid,
        )

        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=timeout_seconds)

        except subprocess.TimeoutExpired:
            self.logger.warning(
                "Process %s did not stop after SIGTERM; sending SIGINT",
                self.name,
            )

            try:
                os.killpg(process.pid, signal.SIGINT)
                process.wait(timeout=5)

            except subprocess.TimeoutExpired:
                self.logger.error(
                    "Process %s did not stop gracefully; sending SIGKILL",
                    self.name,
                )

                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)

        except ProcessLookupError:
            pass

        finally:
            self._close_log()
        
    def _close_log(self) -> None:
        if self._log_handle is not None and not self._log_handle.closed:
            self._log_handle.flush()
            self._log_handle.close()


def wait_for_tcp_port(host: str, port: int, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(0.25)
    raise TimeoutError(f"Timed out waiting for {host}:{port}: {last_error}")
