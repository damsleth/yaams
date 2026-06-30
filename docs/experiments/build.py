#!/usr/bin/env python3
"""Inline experiments.jsonl into index.html so the viewer opens via file://.

The viewer keeps its data in a <script id="experiments"> block; this rewrites
that block's contents from experiments.jsonl. No server, no CDN, no deps.

Run after appending an experiment:  python docs/experiments/build.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).parent
DATA = HERE / "experiments.jsonl"
HTML = HERE / "index.html"

rows = [json.loads(line) for line in DATA.read_text().splitlines() if line.strip()]
payload = json.dumps(rows, ensure_ascii=False)

html = HTML.read_text()
new = re.sub(
    r'(<script id="experiments" type="application/json">).*?(</script>)',
    lambda m: m.group(1) + payload + m.group(2),
    html,
    count=1,
    flags=re.DOTALL,
)
if new == html and payload not in html:
    raise SystemExit("ERROR: data <script id=experiments> block not found in index.html")
HTML.write_text(new)
print(f"inlined {len(rows)} experiments into {HTML.name}")
