from __future__ import annotations

import click

from yaams import __version__


@click.group(invoke_without_command=True)
@click.version_option(__version__, prog_name="yaams")
@click.option(
  "--doctor",
  is_flag=True,
  default=False,
  help="Run health check and exit (data class; pair with --json for machine output).",
)
@click.option(
  "--json",
  "as_json_top",
  is_flag=True,
  default=False,
  help="Machine mode for top-level commands (currently: --doctor).",
)
@click.option(
  "--config",
  "config_path_top",
  default=None,
  help="Path to config.yaml. Honored by top-level --doctor; subcommands take their own --config.",
)
@click.pass_context
def cli(ctx: click.Context, doctor: bool, as_json_top: bool, config_path_top: str | None) -> None:
  if doctor:
    from yaams.cli.doctor import emit_doctor
    ctx.exit(emit_doctor(config_path_top, as_json_top))
  if ctx.invoked_subcommand is None:
    click.echo(ctx.get_help())
    ctx.exit(0)
