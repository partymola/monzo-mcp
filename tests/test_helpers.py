"""Tests for shared helpers."""

import json
import unittest

from monzo_mcp.helpers import format_response, pence_to_pounds


class TestFormatResponse(unittest.TestCase):
    def test_dict(self):
        result = format_response({"key": "value"})
        self.assertEqual(json.loads(result), {"key": "value"})

    def test_list(self):
        result = format_response([1, 2, 3])
        self.assertEqual(json.loads(result), [1, 2, 3])

    def test_none(self):
        result = format_response(None)
        self.assertIsNone(json.loads(result))

    def test_string(self):
        result = format_response("hello")
        self.assertEqual(json.loads(result), {"result": "hello"})


class TestPenceToPounds(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(pence_to_pounds(12345), 123.45)

    def test_negative(self):
        self.assertEqual(pence_to_pounds(-5000), -50.0)

    def test_zero(self):
        self.assertEqual(pence_to_pounds(0), 0.0)


if __name__ == "__main__":
    unittest.main()
