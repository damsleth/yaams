"""MCP server exposing YAAMS Tier-1 query verbs as MCP tools.

Optional: requires the ``mcp`` package (``pip install 'yaams[mcp]'``). Imports
of the server module are lazy so the rest of yaams works without it.
"""

from yaams.mcp.server import create_server, run, scrub_for_egress

__all__ = ["create_server", "run", "scrub_for_egress"]
