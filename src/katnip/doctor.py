from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from katnip.config import AppConfig


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    warning: bool = False


def _command_available(command: str) -> bool:
    expanded = Path(command).expanduser()

    if "/" in command:
        return expanded.is_file() and os.access(expanded, os.X_OK)

    return shutil.which(command) is not None


def _check_alias(shell: str, alias: str) -> CheckResult:
    result = subprocess.run(
        [shell, "-lic", f"alias {alias}"],
        capture_output=True,
        text=True,
        check=False,
    )

    ok = result.returncode == 0 and bool(result.stdout.strip())
    detail = (
        result.stdout.strip()
        if ok
        else result.stderr.strip() or "alias not found"
    )

    return CheckResult(
        f"browser alias: {alias}",
        ok,
        detail,
    )


def _check_python_module(
    module_name: str,
    display_name: str,
) -> CheckResult:
    available = importlib.util.find_spec(module_name) is not None

    return CheckResult(
        display_name,
        available,
        (
            f"Python module {module_name!r} is installed"
            if available
            else f"Python module {module_name!r} is not installed"
        ),
    )


def _check_native_recorder(executable: str) -> CheckResult:
    path = Path(executable).expanduser()

    if "/" not in executable:
        resolved = shutil.which(executable)

        if resolved is None:
            return CheckResult(
                "native window recorder",
                False,
                f"executable not found in PATH: {executable}",
            )

        path = Path(resolved)

    if not path.exists():
        return CheckResult(
            "native window recorder",
            False,
            f"file does not exist: {path}",
        )

    if not path.is_file():
        return CheckResult(
            "native window recorder",
            False,
            f"path is not a file: {path}",
        )

    if not os.access(path, os.X_OK):
        return CheckResult(
            "native window recorder",
            False,
            f"file is not executable: {path}",
        )

    return CheckResult(
        "native window recorder",
        True,
        str(path.resolve()),
    )


def run_doctor(config: AppConfig) -> list[CheckResult]:
    checks: list[CheckResult] = []

    checks.append(
        CheckResult(
            "browser shell",
            _command_available(config.browser.shell),
            config.browser.shell,
        )
    )

    checks.append(
        _check_alias(
            config.browser.shell,
            config.browser.launch_alias,
        )
    )

    if config.mitm.enabled:
        if config.mitm.command_prefix:
            checks.append(
                CheckResult(
                    "mitmproxy command prefix",
                    _command_available(
                        config.mitm.command_prefix[0]
                    ),
                    " ".join(config.mitm.command_prefix),
                )
            )

        checks.append(
            CheckResult(
                "mitmproxy executable",
                _command_available(config.mitm.executable),
                config.mitm.executable,
            )
        )

        if config.mitm.user_script is not None:
            checks.append(
                CheckResult(
                    "user mitmproxy addon",
                    config.mitm.user_script.is_file(),
                    str(config.mitm.user_script),
                )
            )

        if config.mitm.listen_port < 1024:
            checks.append(
                CheckResult(
                    "privileged proxy port",
                    True,
                    (
                        f"port {config.mitm.listen_port} may require "
                        "elevated privileges on macOS"
                    ),
                    warning=True,
                )
            )

    if config.recording.enabled:
        checks.append(
            _check_native_recorder(
                config.recording.executable
            )
        )

        checks.append(
            _check_python_module(
                "Quartz",
                "Quartz window enumeration",
            )
        )

        checks.append(
            CheckResult(
                "Swift toolchain",
                _command_available("swift"),
                (
                    shutil.which("swift")
                    or "swift was not found in PATH"
                ),
            )
        )

        checks.append(
            CheckResult(
                "Screen Recording permission",
                True,
                (
                    "KATNIP cannot reliably verify this permission "
                    "without starting a capture. Ensure the terminal "
                    "or launcher is enabled under System Settings > "
                    "Privacy & Security > Screen & System Audio Recording."
                ),
                warning=True,
            )
        )

    if config.vpn.enabled:
        location = config.vpn.locations[config.test.location]

        checks.append(
            CheckResult(
                f"VPN config for {config.test.location}",
                location.config_path.is_file(),
                str(location.config_path),
            )
        )

        up_executable = config.vpn.up_command[0]

        checks.append(
            CheckResult(
                "VPN command",
                _command_available(up_executable),
                " ".join(config.vpn.up_command),
            )
        )

        checks.append(
            CheckResult(
                "WireGuard executable",
                _command_available("wg"),
                shutil.which("wg") or "wg not found in PATH",
            )
        )

        checks.append(
            CheckResult(
                "wg-quick executable",
                _command_available("wg-quick"),
                (
                    shutil.which("wg-quick")
                    or "wg-quick not found in PATH"
                ),
            )
        )

    output_ancestor = config.run.output_root

    while (
        not output_ancestor.exists()
        and output_ancestor != output_ancestor.parent
    ):
        output_ancestor = output_ancestor.parent

    checks.append(
        CheckResult(
            "output directory",
            (
                output_ancestor.exists()
                and os.access(output_ancestor, os.W_OK)
            ),
            str(config.run.output_root),
        )
    )

    return checks