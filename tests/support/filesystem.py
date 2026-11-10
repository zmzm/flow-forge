"""Temporary directories whose lifetime follows a unittest case."""

import tempfile
import unittest
from pathlib import Path


def temporary_directory(case: unittest.TestCase, prefix: str) -> Path:
    directory = tempfile.TemporaryDirectory(prefix=prefix)
    case.addCleanup(directory.cleanup)
    return Path(directory.name)
