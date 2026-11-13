from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from katnip.config import load_config
from katnip.doctor import run_doctor
from katnip.orchestrator import KATNIPOrchestrator
from katnip.report import generate_report

app = typer.Typer(
    name="katnip",
    help="Config-driven website traffic analysis orchestration.",
    no_args_is_help=True,
)
console = Console()


@app.command()
def run(
    config: Path = typer.Option(..., "--config", "-c", exists=True, dir_okay=False),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run one configured website analysis."""
    try:
        loaded = load_config(config)
        orchestrator = KATNIPOrchestrator(loaded, config, verbose=verbose)
        result = asyncio.run(orchestrator.run())
    except (ValidationError, ValueError) as exc:
        console.print(f"[bold red]Configuration error:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc
    except KeyboardInterrupt as exc:
        console.print("[yellow]Run interrupted.[/yellow]")
        raise typer.Exit(code=130) from exc
    except Exception as exc:
        console.print(f"[bold red]Run failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"[bold green]Run complete:[/bold green] {result.run_dir}")
    console.print(f"Report: {result.report_dir / 'report.md'}")


@app.command()
def doctor(
    config: Path = typer.Option(..., "--config", "-c", exists=True, dir_okay=False),
) -> None:
    """Validate configuration, commands, aliases, and local paths."""
    try:
        loaded = load_config(config)
    except (ValidationError, ValueError) as exc:
        console.print(f"[bold red]Configuration error:[/bold red] {exc}")
        raise typer.Exit(code=2) from exc

    table = Table(title="KATNIP Environment Check")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail")
    failed = False
    for check in run_doctor(loaded):
        if check.warning:
            status = "[yellow]WARN[/yellow]"
        elif check.ok:
            status = "[green]OK[/green]"
        else:
            status = "[red]FAIL[/red]"
            failed = True
        table.add_row(check.name, status, check.detail)
    console.print(table)
    if failed:
        raise typer.Exit(code=1)


@app.command("report")
def report_command(
    run_dir: Path,
) -> None:
    """Regenerate reports for an existing KATNIP run."""

    run_dir = run_dir.expanduser().resolve()

    config_path = run_dir / "config-used.yaml"

    if not config_path.is_file():
        raise typer.BadParameter(
            f"Run does not contain config-used.yaml: {run_dir}"
        )

    flow_path = run_dir / "mitm" / "flows.mitm"

    if not flow_path.is_file():
        raise typer.BadParameter(
            f"Run does not contain flows.mitm: {run_dir}"
        )

    config = load_config(
        config_path
    )

    summary = generate_report(
        run_dir=run_dir,
        website=str(config.test.website),
        config=config.postprocess,
    )

    typer.echo(
        f"Report regenerated: {run_dir / 'report'}"
    )

@app.command("list-journeys")
def list_journeys() -> None:
    """List available and planned journey integrations."""
    table = Table(title="KATNIP Journeys")
    table.add_column("Name")
    table.add_column("Status")
    table.add_column("Description")
    table.add_row("standard", "available", "Load, optional consent action, scroll down/up")
    table.add_row(
        "OneTrust accept/reject",
        "helper available",
        "Configured inside standard via consent.onetrust_action",
    )
    console.print(table)
