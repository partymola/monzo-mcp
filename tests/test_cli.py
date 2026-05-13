"""Tests for CLI argument handling."""

import sys
import unittest
from importlib.metadata import version
from io import StringIO
from unittest.mock import patch

import main


class TestCliVersion(unittest.TestCase):
    def test_version_flag_prints_package_version(self):
        with (
            patch.object(sys, "argv", ["monzo-mcp", "--version"]),
            patch("sys.stdout", new_callable=StringIO) as stdout,
        ):
            with self.assertRaises(SystemExit) as error:
                main.main()

        self.assertEqual(error.exception.code, 0)
        self.assertEqual(stdout.getvalue().strip(), f"monzo-mcp {version('monzo-mcp')}")


if __name__ == "__main__":
    unittest.main()
