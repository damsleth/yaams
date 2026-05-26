from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def yaams_caplog(caplog):
  """Like ``caplog`` but reliable for the ``yaams`` logger tree.

  ``yaams.logsetup.setup_logging`` sets ``propagate=False`` on the ``yaams``
  logger, so once any test triggers it, records never reach caplog's
  root-attached handler. This fixture attaches caplog's handler to the
  ``yaams`` logger directly (records from ``yaams.*`` children propagate up
  to it before being stopped), captures at DEBUG, and restores state after.
  """
  logger = logging.getLogger("yaams")
  prev_level = logger.level
  logger.addHandler(caplog.handler)
  logger.setLevel(logging.DEBUG)
  caplog.set_level(logging.DEBUG, logger="yaams")
  try:
    yield caplog
  finally:
    logger.removeHandler(caplog.handler)
    logger.setLevel(prev_level)
