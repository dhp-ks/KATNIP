from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from katnip.browser import BrowserManager
from katnip.config import AppConfig
from katnip.journeys.standard import StandardJourney
from katnip.journeys.click import ClickJourney
from katnip.journeys.manual import ManualJourney
from katnip.logging_setup import configure_logging
from katnip.mitm import MitmManager
from katnip.recording import RecordingManager
from katnip.precondition import BrowserPreconditioner
from katnip.report import generate_report
from katnip.utils import safe_slug, utc_now_iso, write_json
from katnip.vpn import VPNManager


@dataclass
class RunResult:
    run_dir: Path
    report_dir: Path
    summary_path: Path


class KATNIPOrchestrator:
    def __init__(
        self,
        config: AppConfig,
        config_path: Path,
        verbose: bool = False,
    ) -> None:
        self.config = config
        self.config_path = config_path.resolve()
        self.run_dir = self._create_run_dir()
        self.logger = configure_logging(self.run_dir / "logs" / "katnip.log", verbose)

        self.vpn = VPNManager(
            config.vpn,
            config.test.location,
            self.run_dir / "logs" / "vpn.log",
        )
        
        self.browser_window_marker = f"{uuid4().hex}"
        self.browser = BrowserManager(
            config.browser,
            self.run_dir,
            self.run_dir / "logs" / "browser.log",
            window_marker=self.browser_window_marker
        )
        self.recording = RecordingManager(config.recording, self.run_dir, window_marker=self.browser_window_marker)
        self.mitm = MitmManager(config.mitm, self.run_dir)

    def _create_run_dir(self) -> Path:
        host = urlsplit(self.config.test.website).hostname or "website"
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        name = safe_slug(
            f"{timestamp}-{host}-{self.config.test.location}-{self.config.test.journey}"
        )
        run_dir = self.config.run.output_root / name
        run_dir.mkdir(parents=True, exist_ok=False)
        for child in ("logs", "mitm", "recording", "report"):
            (run_dir / child).mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.config_path, run_dir / "config-used.yaml")
        return run_dir

    async def run(self) -> RunResult:
        metadata: dict[str, object] = {
            "run_name": self.config.run.name,
            "status": "running",
            "started_at": utc_now_iso(),
            "website": self.config.test.website,
            "location": self.config.test.location,
            "journey": self.config.test.journey,
            "run_dir": str(self.run_dir),
        }
        error: BaseException | None = None
        cleanup_errors: list[str] = []
        report_summary: dict[str, object] | None = None

        self.logger.info("KATNIP run directory: %s", self.run_dir)
        write_json(self.run_dir / "run_summary.json", metadata)

        try:
            metadata["public_ip_before_vpn"] = self.vpn.public_ip()
            self.vpn.connect()
            metadata["public_ip_after_vpn"] = self.vpn.public_ip()
            if (
                self.config.vpn.enabled
                and metadata["public_ip_before_vpn"]
                and metadata["public_ip_before_vpn"] == metadata["public_ip_after_vpn"]
            ):
                self.logger.warning("Public IP did not change after VPN connection")

            # Launch to about:blank first so the configured proxy can start before navigation.
            self.browser.launch()
            self.recording.start()
            if self.config.recording.enabled:
                await asyncio.sleep(self.config.recording.startup_wait_seconds)
                if self.recording.process is not None:
                    self.recording.process.ensure_running()

            if self.config.mitm.enabled:
                self.logger.info("Proxy enabled by configuration")
                self.mitm.start()
            if self.mitm.process is None:
                self.logger.warning("Proxy failed to start")
            else:
                self.logger.info("Proxy disabled by configuration")
            page = await self.browser.connect()
            
            preconditioner = BrowserPreconditioner(
                self.config.precondition,
            )
            await preconditioner.run(page)
            if self.config.precondition.enabled:
                self.logger.info(
                    "Resetting capture after browser preconditioning"
                )

                await asyncio.to_thread(
                    self.mitm.reset_capture,
                    archive_name="precondition",
                    next_phase="pre_consent",
                )

            async def reset_mitm_capture() -> None:
                await asyncio.to_thread(
                    self.mitm.reset_capture,
                    archive_name="pre_consent",
                    next_phase="post_consent",
                )
                
            async def clear_browser_cache() -> None:
                await self.browser.clear_cache()

            journey_name = self.config.test.journey

            self.logger.info(
                "Creating journey | journey=%s",
                journey_name,
            )

            if journey_name == "standard":
                journey = StandardJourney(
                    self.config.journey_settings,
                    self.config.consent,
                    self.run_dir,
                    reset_capture=reset_mitm_capture,
                    clear_browser_cache=clear_browser_cache,
                    visual_cursor=self.browser.visual_cursor,
                )
                
            elif journey_name == "click":
                journey = ClickJourney(
                    self.config.journey_settings,
                    self.config.consent,
                    reset_capture=reset_mitm_capture,
                    clear_browser_cache=clear_browser_cache,
                    visual_cursor=self.browser.visual_cursor,
                )

            elif journey_name == "manual":
                journey = ManualJourney(
                    self.config.journey_settings,
                    self.config.consent,
                    self.run_dir,
                    reset_capture=reset_mitm_capture,
                    clear_browser_cache=clear_browser_cache,
                    visual_cursor=self.browser.visual_cursor,
                )

            else:
                raise ValueError(
                    f"Unsupported journey: {journey_name}"
                )

            self.logger.info(
                "Starting journey | journey=%s",
                journey_name,
            )

            await journey.run(
                page,
                str(self.config.test.website),
            )
            
            metadata["journey_completed_at"] = utc_now_iso()
            metadata["status"] = "completed"
        except BaseException as exc:
            error = exc
            metadata["status"] = "failed"
            metadata["error"] = f"{type(exc).__name__}: {exc}"
            self.logger.exception("KATNIP run failed")
        finally:
            cleanup_steps = [
                ("screen recording", self.recording.stop),
                ("mitmproxy", self.mitm.stop),
            ]
            for name, action in cleanup_steps:
                try:
                    action()
                except Exception as exc:
                    message = f"Failed to stop {name}: {exc}"
                    cleanup_errors.append(message)
                    self.logger.exception(message)

            try:
                await self.browser.close()
            except Exception as exc:
                message = f"Failed to close Chrome: {exc}"
                cleanup_errors.append(message)
                self.logger.exception(message)

            try:
                self.vpn.disconnect()
            except Exception as exc:
                message = f"Failed to disconnect VPN: {exc}"
                cleanup_errors.append(message)
                self.logger.exception(message)

            try:
                report_summary = generate_report(
                    self.run_dir,
                    self.config.test.website,
                    self.config.postprocess,
                )
            except Exception as exc:
                message = f"Failed to generate report: {exc}"
                cleanup_errors.append(message)
                self.logger.exception(message)
                if error is None and self.config.run.fail_on_report_error:
                    error = exc
                    metadata["status"] = "failed"

            if (
                self.config.browser.clean_profile
                and self.browser.profile_dir.exists()
                and not self.config.run.keep_chrome_profile
            ):
                try:
                    shutil.rmtree(self.browser.profile_dir)
                except OSError as exc:
                    cleanup_errors.append(f"Failed to remove Chrome profile: {exc}")

            metadata["finished_at"] = utc_now_iso()
            metadata["cleanup_errors"] = cleanup_errors
            if report_summary is not None:
                metadata["report_summary"] = {
                    key: value
                    for key, value in report_summary.items()
                    if key != "third_parties"
                }
            if cleanup_errors and metadata["status"] == "completed":
                metadata["status"] = "completed_with_cleanup_warnings"
            write_json(self.run_dir / "run_summary.json", metadata)

        if error is not None:
            raise error

        return RunResult(
            run_dir=self.run_dir,
            report_dir=self.run_dir / "report",
            summary_path=self.run_dir / "run_summary.json",
        )
