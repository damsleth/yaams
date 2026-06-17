from __future__ import annotations

import click

from yaams.cli._root import cli
from yaams.cli._shared import config_option


@cli.command("mcp")
@click.option(
  "--allow-write",
  is_flag=True,
  default=False,
  help="Expose the write-gated yaams_feedback tool (off by default).",
)
@config_option
def mcp_cmd(allow_write: bool, config_path: str) -> None:
  """Run the YAAMS MCP server over stdio.

  Exposes Tier-1 query verbs (yaams_query, yaams_answer) as MCP tools so any
  MCP client can search the raw store directly. Requires the optional 'mcp'
  package: pip install 'yaams[mcp]'.
  """
  from yaams import mcp as mcp_pkg

  try:
    mcp_pkg.run(config_path=config_path, allow_write=allow_write)
  except RuntimeError as exc:  # missing optional 'mcp' package
    raise click.ClickException(str(exc)) from exc
