#!/usr/bin/env python3
"""Compatibility CLI: legacy environment or explicitly selected FlowForge project."""

import sys

from flowforge_cli import main

if __name__ == "__main__":
    sys.exit(main(["run", *sys.argv[1:]], allow_legacy=True))
