#!/usr/bin/env python
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yaams.cli import consolidate

if __name__ == "__main__":
  consolidate()
