#!/usr/bin/env python3
"""Run the repository's quality checks with the current Python interpreter."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stdlib-only",
        action="store_true",
        help="Compile and test without development or UI tools",
    )
    args = parser.parse_args(argv)
    commands = []
    if not args.stdlib_only:
        commands.extend(
            [
                ["-m", "ruff", "check", "."],
                ["-m", "ruff", "format", "--check", "."],
                ["-m", "mypy"],
            ]
        )
    sources = [*ROOT.glob("*.py"), ROOT / "flowforge"]
    for directory in ("notifications", "tests", "scripts"):
        sources.extend((ROOT / directory).rglob("*.py"))
    commands.extend(
        [
            ["-m", "py_compile", *(str(path.relative_to(ROOT)) for path in sorted(sources))],
            ["-m", "unittest", "discover", "-s", "tests", "-t", "."],
        ]
    )
    for command in commands:
        print(f"Running: {sys.executable} {' '.join(command)}", flush=True)
        result = subprocess.run([sys.executable, *command], cwd=ROOT, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
