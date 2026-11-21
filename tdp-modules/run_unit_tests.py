"""Dedicated unit-test runner for TvC Continuo.

This runner intentionally uses only the Python standard library so it can run
inside the existing project virtualenv without installing pytest.

Usage from repo root:
    python tdp-modules/run_unit_tests.py

Usage from tdp-modules:
    python run_unit_tests.py
"""
from __future__ import annotations

import argparse
import os
import sys
import unittest

# The repository's packaging checks inspect the application tree for cache
# artifacts. Test discovery should not create new bytecode files there.
sys.dont_write_bytecode = True


def _module_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run TvC Continuo unit tests.")
    parser.add_argument(
        "pattern",
        nargs="?",
        default="test_*.py",
        help="unittest discovery pattern, default: test_*.py",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="use quieter unittest output",
    )
    args = parser.parse_args(argv)

    app_dir = _module_dir()
    tests_dir = os.path.join(app_dir, "tests")

    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)

    os.chdir(app_dir)
    print(f"[unit tests] app_dir={app_dir}")
    print(f"[unit tests] tests_dir={tests_dir}")
    print(f"[unit tests] pattern={args.pattern}")

    loader = unittest.defaultTestLoader
    suite = loader.discover(start_dir=tests_dir, pattern=args.pattern, top_level_dir=app_dir)
    verbosity = 1 if args.quiet else 2
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=verbosity).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
