#!/usr/bin/env python3
"""Compatibility launcher for flowforge_watch."""

from flowforge_watch import build_status as build_status
from flowforge_watch import main
from flowforge_watch import render_report as render_report

if __name__ == "__main__":
    raise SystemExit(main())
