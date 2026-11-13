from __future__ import annotations

import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class RunConfig(BaseModel):
    name: str = "website-analysis"
    output_root: Path = Path("output")
    keep_chrome_profile: bool = False
    fail_on_report_error: bool = False


class TestConfig(BaseModel):
    website: str
    location: str
    journey: Literal[
        "standard",
        "click",
        "manual",
    ] = "standard"

    @field_validator("website")
    @classmethod
    def validate_website(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("website must be a complete http:// or https:// URL")
        return value

    @field_validator("location")
    @classmethod
    def normalize_location(cls, value: str) -> str:
        return value.strip().upper()


class VPNLocationConfig(BaseModel):
    config_path: Path


class VPNConfig(BaseModel):
    enabled: bool = True
    driver: Literal["wireguard", "shell"] = "wireguard"
    locations: dict[str, VPNLocationConfig]
    up_command: list[str] = Field(
        default_factory=lambda: ["wg-quick", "up", "{config_path}"],
        min_length=1,
    )
    down_command: list[str] = Field(
        default_factory=lambda: ["wg-quick", "down", "{config_path}"],
        min_length=1,
    )
    connect_timeout_seconds: float = 30.0
    verify_public_ip: bool = True
    public_ip_check_url: str = "https://api.ipify.org"
    public_ip_timeout_seconds: float = 10.0


class MitmConfig(BaseModel):
    enabled: bool = True
    command_prefix: list[str] = Field(default_factory=list)
    executable: str = "mitmdump"
    listen_host: str = "127.0.0.1"
    listen_port: int = 443
    user_script: Path | None = None
    extra_args: list[str] = Field(default_factory=list)
    startup_timeout_seconds: float = 15.0
    save_flow_file: bool = True

    @field_validator("listen_port")
    @classmethod
    def validate_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("listen_port must be between 1 and 65535")
        return value


class BrowserConfig(BaseModel):
    launch_alias: str = "mitmchrome"
    shell: str = "/bin/zsh"
    remote_debugging_host: str = "127.0.0.1"
    remote_debugging_port: int = 9222
    clean_profile: bool = True
    startup_timeout_seconds: float = 25.0
    launch_extra_args: list[str] = Field(
        default_factory=lambda: [
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-default-apps",
        ]
    )
    close_processes_on_exit: bool = True


class RecordingConfig(BaseModel):
    enabled: bool = True
    executable: str = "ffmpeg"
    input_device: str = "1"
    fps: int = 30
    mode: Literal["chrome_window", "full_screen"] = "chrome_window"
    window_scale_factor: float = 2.0
    include_cursor: bool = True
    startup_wait_seconds: float = 1.0
    video_codec: str = "libx264"
    preset: str = "veryfast"
    crf: int = 23
    extra_args: list[str] = Field(default_factory=list)


class ScrollConfig(BaseModel):
    cycles: int = 2
    steps_per_direction: int = 4
    min_delta: int = 260
    max_delta: int = 760
    pause_min_seconds: float = 0.35
    pause_max_seconds: float = 1.0


class HumanMotionConfig(BaseModel):
    enabled: bool = True
    mouse_moves_per_cycle: int = 4
    min_step_delay_seconds: float = 0.01
    max_step_delay_seconds: float = 0.04


class JourneySettingsConfig(BaseModel):
    page_load_timeout_seconds: float = 45.0
    initial_wait_min_seconds: float = 2.0
    initial_wait_max_seconds: float = 4.0
    final_wait_min_seconds: float = 1.0
    final_wait_max_seconds: float = 2.0
    scroll: ScrollConfig = Field(default_factory=ScrollConfig)
    human_motion: HumanMotionConfig = Field(default_factory=HumanMotionConfig)


class ConsentConfig(BaseModel):
    action: Literal["none", "accept", "reject"] = "none"

    timeout_seconds: float = Field(
        default=5.0,
        gt=0,
    )

    post_action_wait_seconds: float = Field(
        default=3.0,
        ge=0,
    )


class PostprocessConfig(BaseModel):
    include_query_in_sample_urls: bool = False
    max_sample_urls_per_domain: int = 3
    ignore_hosts: list[str] = Field(
        default_factory=lambda: ["127.0.0.1", "localhost"]
    )
    

class PreconditionSiteConfig(BaseModel):
    url: str

    wait_seconds: float = Field(
        default=3.0,
        ge=0,
    )


class PreconditionConfig(BaseModel):
    enabled: bool = False

    sites: list[PreconditionSiteConfig] = Field(
        default_factory=list,
    )


class AppConfig(BaseModel):
    run: RunConfig = Field(default_factory=RunConfig)
    test: TestConfig
    vpn: VPNConfig
    mitm: MitmConfig = Field(default_factory=MitmConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    recording: RecordingConfig = Field(default_factory=RecordingConfig)
    journey_settings: JourneySettingsConfig = Field(default_factory=JourneySettingsConfig)
    consent: ConsentConfig = Field(default_factory=ConsentConfig)
    postprocess: PostprocessConfig = Field(default_factory=PostprocessConfig)
    precondition: PreconditionConfig = Field(
        default_factory=PreconditionConfig,
    )

    @model_validator(mode="after")
    def validate_location(self) -> "AppConfig":
        normalized_locations = {key.upper(): value for key, value in self.vpn.locations.items()}
        self.vpn.locations = normalized_locations
        if self.vpn.enabled and self.test.location not in normalized_locations:
            available = ", ".join(sorted(normalized_locations)) or "none"
            raise ValueError(
                f"test.location={self.test.location!r} has no VPN mapping; available: {available}"
            )
        return self


def _resolve_path(path: Path, base_dir: Path) -> Path:
    expanded = Path(os.path.expandvars(str(path))).expanduser()
    if not expanded.is_absolute():
        expanded = base_dir / expanded
    return expanded.resolve()


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    config = AppConfig.model_validate(raw)
    base_dir = config_path.parent

    config.run.output_root = _resolve_path(config.run.output_root, base_dir)
    if config.mitm.user_script is not None:
        config.mitm.user_script = _resolve_path(config.mitm.user_script, base_dir)
    for location in config.vpn.locations.values():
        location.config_path = _resolve_path(location.config_path, base_dir)

    return config


