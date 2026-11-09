"""Einstiegspunkt für `python -m flashscore`."""

from __future__ import annotations

import sys

from flashscore.cli import main

if __name__ == "__main__":
    sys.exit(main())
