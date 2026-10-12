"""Log redaction and rate-limiter tests."""
import json
import logging
import os
import tempfile
import unittest
from unittest import mock

from tests.helpers import FakeMonotonic

from logging_config import get_logger, log_event, mask_text, setup_logging
from rate_limiter import SlidingWindowCounter, TokenBucket
from secrets_resolver import resolve_string


class RedactionTests(unittest.TestCase):
    def test_mask_text(self):
        self.assertEqual(mask_text("Authorization: Bearer abcdef123456"), "Authorization: Bearer ***")
        self.assertNotIn("hunter22", mask_text("password=hunter22&x=1"))
        self.assertEqual(mask_text("acct 1234567890123456 ok"), "acct ************3456 ok")
        self.assertEqual(mask_text("HTTP 503 in 1200 ms"), "HTTP 503 in 1200 ms")

    def test_resolved_secrets_are_redacted_everywhere(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"LOG_T_SECRET": "Zq9-secret-value"}):
            resolve_string("${env:LOG_T_SECRET}")
            setup_logging({"directory": tmp, "level": "DEBUG"}, "DEV")
            logger = get_logger("t")
            log_event(logger, logging.INFO, "EVENT with Zq9-secret-value", detail="x Zq9-secret-value y",
                      password="plain", nested={"api_key": "k", "list": ["Zq9-secret-value"]})
            try:
                raise ValueError("failed with Zq9-secret-value")
            except ValueError:
                logger.error("boom", exc_info=True)
            for handler in logging.getLogger("apimon").handlers:
                handler.flush()
            with open(os.path.join(tmp, "monitor.log")) as handle:
                text = handle.read()
        self.assertNotIn("Zq9-secret-value", text)
        first = json.loads(text.splitlines()[0])
        self.assertEqual(first["password"], "***")
        self.assertEqual(first["nested"]["api_key"], "***")
        self.assertEqual(first["environment"], "DEV")


class RateLimiterTests(unittest.TestCase):
    def test_token_bucket(self):
        clock = FakeMonotonic()
        bucket = TokenBucket(60, clock=clock)  # 1 per second, burst 60
        self.assertEqual(sum(bucket.try_acquire() for _ in range(100)), 60)
        clock.advance(5)
        self.assertEqual(sum(bucket.try_acquire() for _ in range(100)), 5)
        self.assertFalse(TokenBucket(0, clock=clock).try_acquire())

    def test_sliding_window(self):
        clock = FakeMonotonic()
        window = SlidingWindowCounter(3, 3600, clock=clock)
        self.assertEqual([window.try_acquire() for _ in range(4)], [True, True, True, False])
        clock.advance(3600)
        self.assertTrue(window.try_acquire())


if __name__ == "__main__":
    unittest.main()
