#!/usr/bin/env python3
"""Compatibility launcher; help does not require optional UI dependencies."""

from flowforge_cli import ui_main

if __name__ == "__main__":
    raise SystemExit(ui_main())
