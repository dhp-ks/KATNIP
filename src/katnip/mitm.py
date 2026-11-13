from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Literal

from katnip.config import MitmConfig
from katnip.process import ManagedProcess, wait_for_tcp_port


CapturePhase = Literal["pre_consent", "post_consent"]


class MitmManager:
    def __init__(
        self,
        config: MitmConfig,
        run_dir: Path,
    ) -> None:
        self.config = config
        self.run_dir = run_dir

        self.output_dir = run_dir / "mitm"
        self.log_path = run_dir / "logs" / "mitmproxy.log"

        self.flow_path = self.output_dir / "flows.mitm"
        self.har_path = self.output_dir / "network.har"
        self.request_log_path = (
            self.output_dir / "capture_requests.jsonl"
        )

        self.capture_phase: CapturePhase = "pre_consent"
        self.process: ManagedProcess | None = None

        self.logger = logging.getLogger("katnip.mitm")

    def _bundled_addon_path(self) -> Path:
        return (
            Path(__file__).resolve().parent
            / "addons"
            / "capture_requests.py"
        )

    def _capture_paths(self) -> tuple[Path, ...]:
        return (
            self.request_log_path,
            self.flow_path,
            self.har_path,
        )

    def _remove_active_capture_files(self) -> None:
        """
        Remove stale files before starting a new capture phase.

        This prevents a restarted mitmproxy process from reusing output
        left by a previous process or failed run.
        """

        for path in self._capture_paths():
            if not path.exists():
                continue

            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)

            self.logger.debug(
                "Removed stale capture artifact: %s",
                path,
            )

    def _archive_pre_consent_capture(self) -> None:
        pre_consent_dir = self.output_dir / "pre_consent"

        if pre_consent_dir.exists():
            shutil.rmtree(pre_consent_dir)

        pre_consent_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        for source in self._capture_paths():
            if not source.exists():
                continue

            destination = pre_consent_dir / source.name

            shutil.move(
                str(source),
                str(destination),
            )

            self.logger.info(
                "Archived pre-consent capture: %s",
                destination,
            )

    def reset_capture(
        self,
        *,
        archive_name: str,
        next_phase: str,
    ) -> None:
        self.logger.info(
            "Resetting mitmproxy capture "
            "| archive=%s "
            "| next_phase=%s",
            archive_name,
            next_phase,
        )

        self.stop()

        archive_dir = (
            self.output_dir
            / archive_name
        )

        archive_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        for source in self._capture_paths():
            if not source.exists():
                continue

            destination = (
                archive_dir
                / source.name
            )

            if destination.exists():
                destination.unlink()

            shutil.move(
                str(source),
                str(destination),
            )

        self.capture_phase = next_phase

        self.start()

    def build_command(self) -> list[str]:
        command = [
            *self.config.command_prefix,
            self.config.executable,
            "--listen-host",
            self.config.listen_host,
            "--listen-port",
            str(self.config.listen_port),
        ]

        mitm_options: dict[str, str | Path] = {
            "katnip_output_dir": self.output_dir,
        }

        # Only generate the final HAR for the capture phase used by the
        # report. The pre-consent flow file is still archived separately.
        # if self.capture_phase == "post_consent":
        if True:    
            mitm_options["hardump"] = self.har_path

        for key, value in mitm_options.items():
            command.extend(
                [
                    "--set",
                    f"{key}={value}",
                ]
            )

        bundled_addon = self._bundled_addon_path()

        if not bundled_addon.is_file():
            raise FileNotFoundError(
                f"Bundled mitmproxy addon not found: {bundled_addon}"
            )

        command.extend(
            [
                "-s",
                str(bundled_addon),
            ]
        )

        if self.config.user_script is not None:
            if not self.config.user_script.is_file():
                raise FileNotFoundError(
                    "Configured mitmproxy addon not found: "
                    f"{self.config.user_script}"
                )

            command.extend(
                [
                    "-s",
                    str(self.config.user_script),
                ]
            )

        if self.config.save_flow_file:
            command.extend(
                [
                    "-w",
                    str(self.flow_path),
                ]
            )

        command.extend(self.config.extra_args)

        return command

    def start(self) -> None:
        if not self.config.enabled:
            self.logger.info(
                "mitmproxy disabled by configuration"
            )
            return

        if (
            self.process is not None
            and self.process.process is not None
            and self.process.process.poll() is None
        ):
            raise RuntimeError(
                "Cannot start mitmproxy because it is already running"
            )

        self.output_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.log_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self._remove_active_capture_files()

        command = self.build_command()

        self.logger.info(
            "Starting mitmproxy "
            "| phase=%s "
            "| host=%s "
            "| port=%d",
            self.capture_phase,
            self.config.listen_host,
            self.config.listen_port,
        )

        self.logger.debug(
            "mitmproxy command: %r",
            command,
        )

        self.process = ManagedProcess(
            name="mitmproxy",
            command=command,
            log_path=self.log_path,
            cwd=self.output_dir,
        )

        try:
            self.process.start()
            self.process.ensure_running()

            wait_for_tcp_port(
                self.config.listen_host,
                self.config.listen_port,
                self.config.startup_timeout_seconds,
            )

            self.process.ensure_running()

        except Exception:
            self.logger.exception(
                "mitmproxy failed to start; see %s",
                self.log_path,
            )

            if self.process is not None:
                self.process.stop(timeout_seconds=5)

            self.process = None
            raise

        self.logger.info(
            "mitmproxy listening on %s:%s "
            "| phase=%s",
            self.config.listen_host,
            self.config.listen_port,
            self.capture_phase,
        )

    def stop(self) -> None:
        process = self.process

        if process is None:
            return

        self.logger.info(
            "Stopping mitmproxy "
            "| phase=%s",
            self.capture_phase,
        )

        try:
            process.stop(timeout_seconds=30)
        finally:
            self.process = None

        # The HAR is written when mitmproxy exits, so verify it only for
        # the phase where hardump is enabled.
        if self.capture_phase == "post_consent":
            if self.har_path.is_file():
                self.logger.info(
                    "HAR written to %s",
                    self.har_path,
                )
            else:
                self.logger.warning(
                    "mitmproxy stopped but HAR was not created: %s",
                    self.har_path,
                )