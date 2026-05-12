from __future__ import annotations

import click

from yaams import __version__


@click.group()
@click.version_option(__version__, prog_name="yaams")
def cli() -> None:
  pass
