# MCP Server

YAAMS ships an [MCP](https://modelcontextprotocol.io) server that exposes the
Tier-1 query verbs as tools any MCP client (Claude Desktop, Claude Code,
Cursor, Cline, or another agent) can call natively. It is the zero-glue way to
let an assistant search your raw digital exhaust.

The server talks **stdio** and is local-first: no network listener, no auth, no
daemon. It reads the same `config.yaml` and SQLite store the CLI uses.

## Install and run

The `mcp` dependency is optional. Install the extra, then launch:

```bash
pip install 'yaams[mcp]'        # or: uv pip install 'yaams[mcp]'
yaams mcp                       # read-only tools over stdio
yaams mcp --allow-write         # also expose the write-gated feedback tool
yaams mcp --config /path/to/config.yaml
```

`yaams mcp` blocks, serving over stdio — it is meant to be spawned by an MCP
client, not run interactively. If the `mcp` package is missing, the command
exits with an actionable install hint.

### Client configuration

Point your MCP client at the command. For example, in a Claude Desktop /
Claude Code MCP config:

```json
{
  "mcpServers": {
    "yaams": {
      "command": "yaams",
      "args": ["mcp", "--config", "/Users/you/.config/yaams/config.yaml"]
    }
  }
}
```

Add `"--allow-write"` to the `args` array to enable `yaams_feedback`.

## Tools

| tool | gate | what it does |
| --- | --- | --- |
| `yaams_query` | read | Ranked hybrid (FTS + vector) search over Tier-1. Returns results with [trust verdicts](#trust-verdicts). |
| `yaams_answer` | read | Synthesizes a grounded, cited answer over the top results (uses the configured LLM backend). |
| `yaams_feedback` | **write** | Logs a relevance signal against a prior query result. Only registered with `--allow-write`. |

### `yaams_query(query, limit=10, tier="both", source="")`

- `tier`: `both` (default), `raw` (everything except the curated Tier-2
  ledger), or `ledger` (only Tier-2 notes).
- `source`: restrict to a single ingest source (e.g. `email`, `github`,
  `imessage`, `chats`). Overrides `tier` when set.

Returns `{"results": [ ... ]}` where each result mirrors the
`yaams query --format json` shape and carries a `trust` object.

### `yaams_answer(question, limit=5, tier="both")`

Runs the same retrieval, then synthesizes an answer. Returns the answer body,
`confidence` / `confidence_reason`, `gaps`, `cited_ranks` / `cited_result_ids`,
the `backend` / `model` used, and the underlying `results`.

### `yaams_feedback(query_id, rank, verdict, note="")`

Write-gated. `verdict` is one of `hit | relevant | miss | correction | thin`.
`rank` is the 1-based position from the originating query; the server resolves
it to the result id (via `query_results`) so the signal is attributable and
feeds future trust verdicts. Returns `{"logged": true, ...}`.

## Privacy: egress scrub

Every tool response is routed through `scrub_for_egress`, which recursively
strips `<private>…</private>` spans from all strings before they leave the
process — defense-in-depth so fenced private content never reaches a client.
See [privacy-security.md](privacy-security.md).

## Relationship to cognitive-ledger

cognitive-ledger (Tier 2) has its own MCP server and previously wrapped YAAMS
by shelling out to `yaams query` in a `yaams_query` tool. With a first-party
YAAMS MCP server, clients can query Tier 1 directly; the subprocess shim is no
longer the only path in.

## Implementation

- `yaams/mcp/server.py` — `create_server()`, `run()`, `scrub_for_egress()`.
- `yaams/cli/mcp.py` — the `yaams mcp` Click command.
- Tools reuse the same retrieval path as the CLI (`yaams.retrieve.query` +
  `attach_trust_verdicts`) and the same JSON serialization (`_result_to_dict`).
