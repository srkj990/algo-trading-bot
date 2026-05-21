from __future__ import annotations

import unittest
from unittest.mock import patch

from network_utils import create_requests_session


class NetworkUtilsTests(unittest.TestCase):
    def test_create_requests_session_applies_default_timeout(self) -> None:
        with patch("network_utils.get_broker_request_timeout_seconds", return_value=12.5), \
             patch("requests.sessions.Session.request", autospec=True, return_value="ok") as request_mock:
            session = create_requests_session()
            response = session.request("GET", "https://example.com")

        self.assertEqual(response, "ok")
        _, kwargs = request_mock.call_args
        self.assertEqual(kwargs["timeout"], 12.5)

    def test_create_requests_session_preserves_explicit_timeout(self) -> None:
        with patch("network_utils.get_broker_request_timeout_seconds", return_value=12.5), \
             patch("requests.sessions.Session.request", autospec=True, return_value="ok") as request_mock:
            session = create_requests_session()
            response = session.request("GET", "https://example.com", timeout=5)

        self.assertEqual(response, "ok")
        _, kwargs = request_mock.call_args
        self.assertEqual(kwargs["timeout"], 5)


if __name__ == "__main__":
    unittest.main()
