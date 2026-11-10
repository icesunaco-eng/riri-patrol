#!/usr/bin/env python3
"""Run the full suite with socket creation/resolution blocked, no OS settings changed."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def forbidden(*args, **kwargs):
    raise AssertionError("Network access is forbidden in this test suite")


def main():
    with patch("socket.socket", forbidden), patch("socket.create_connection", forbidden), patch("socket.getaddrinfo", forbidden):
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
