#!/usr/bin/env python3
"""Shutdown must work without installing dependencies or rebuilding the UI."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gleskit_mcp.web.lifecycle import main

if __name__ == "__main__":
    sys.argv[1:1] = ["--project-root", str(ROOT)]
    main()
