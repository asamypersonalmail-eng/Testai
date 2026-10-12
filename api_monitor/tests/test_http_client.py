"""Secure HTTP client tests against a local mock server (real sockets)."""
import os
import socket
import unittest
from unittest import mock

from tests.helpers import MockServer, closed_port, json_reply

import requests

from http_client import RequestSpec, SecureHttpClient, classify_exception
from models import FailureClass

SECURITY = {"allowed_hosts": ["127.0.0.1:*", "localhost:*"], "allowed_schemes": ["http", "https"],
            "allowed_methods": ["GET", "POST"], "allow_redirects": True, "forbidden_url_keywords": ["transfer"],
            "use_environment_proxies": False, "proxies": {}, "ca_bundle": None}


def make_spec(url, **kw):
    base = dict(url=url, method="GET", headers={}, query_params={}, body=None, auth={"type": "none"},
                connect_timeout=1.0, read_timeout=1.0, total_timeout=3.0)
    base.update(kw)
    return RequestSpec(**base)


class HttpClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = MockServer().__enter__()
        cls.client = SecureHttpClient(SECURITY, max_response_bytes=4096)

    @classmethod
    def tearDownClass(cls):
        cls.server.__exit__(None, None, None)

    def test_success_with_secrets_and_body(self):
        self.server.routes["/ok"] = json_reply({"status": "SUCCESS"})
        with mock.patch.dict(os.environ, {"T_TOKEN": "tok-123456", "T_ACCT": "9988776655"}):
            spec = make_spec(self.server.url("/ok"), method="POST",
                             headers={"X-Req": "${gen:uuid}"}, query_params={"q": "1"},
                             auth={"type": "bearer", "token": "${env:T_TOKEN}"},
                             body={"type": "json", "content": {"account": "${env:T_ACCT}"}})
            result = self.client.execute(spec)
        self.assertIsNone(result.classification, result.detail)
        self.assertEqual(result.response.status_code, 200)
        req = self.server.requests_for("/ok")[-1]
        self.assertEqual(req["headers"]["Authorization"], "Bearer tok-123456")
        self.assertIn(b"9988776655", req["body"])
        self.assertIn("q=1", req["raw_path"])
        self.assertNotIn("${gen:uuid}", req["headers"]["X-Req"])

    def test_basic_and_api_key_auth(self):
        self.server.routes["/auth"] = json_reply({})
        with mock.patch.dict(os.environ, {"T_PW": "pw-secret", "T_KEY": "key-secret"}):
            self.client.execute(make_spec(self.server.url("/auth"),
                                          auth={"type": "basic", "username": "u", "password": "${env:T_PW}"}))
            self.client.execute(make_spec(self.server.url("/auth"),
                                          auth={"type": "api_key", "header": "X-API-Key", "value": "${env:T_KEY}"}))
        reqs = self.server.requests_for("/auth")
        self.assertTrue(reqs[-2]["headers"]["Authorization"].startswith("Basic "))
        self.assertEqual(reqs[-1]["headers"]["X-API-Key"], "key-secret")

    def test_http_500_and_503_are_returned_for_validation(self):
        for code in (500, 503):
            self.server.routes["/err"] = json_reply({"error": "x"}, status=code)
            result = self.client.execute(make_spec(self.server.url("/err")))
            self.assertEqual(result.response.status_code, code)

    def test_connection_refused(self):
        result = self.client.execute(make_spec(f"http://127.0.0.1:{closed_port()}/x"))
        self.assertEqual(result.classification, FailureClass.CONNECTION_ERROR)
        self.assertEqual(result.detail, "connection refused")

    def test_dns_failure(self):
        def fail(*_a, **_k):
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        with mock.patch("socket.getaddrinfo", side_effect=fail):
            result = self.client.execute(make_spec("http://localhost:8080/x"))
        self.assertEqual(result.classification, FailureClass.DNS_ERROR)

    def test_tls_handshake_error(self):
        self.server.routes["/tls"] = json_reply({})
        result = self.client.execute(make_spec(f"https://127.0.0.1:{self.server.port}/tls"))
        self.assertEqual(result.classification, FailureClass.TLS_ERROR)

    def test_read_timeout(self):
        self.server.routes["/slow"] = json_reply({"status": "SUCCESS"}, delay=1.5)
        result = self.client.execute(make_spec(self.server.url("/slow"), read_timeout=0.3, total_timeout=1.0))
        self.assertEqual(result.classification, FailureClass.TIMEOUT)
        self.assertLess(result.elapsed_ms, 1400)

    def test_response_too_large(self):
        self.server.routes["/big"] = (200, {"Content-Type": "application/json"}, "x" * 10000, 0)
        result = self.client.execute(make_spec(self.server.url("/big")))
        self.assertEqual(result.classification, FailureClass.RESPONSE_TOO_LARGE)

    def test_redirect_not_followed_by_default(self):
        self.server.routes["/r"] = (302, {"Location": "/ok"}, "", 0)
        result = self.client.execute(make_spec(self.server.url("/r")))
        self.assertEqual(result.response.status_code, 302)

    def test_redirect_followed_only_to_allowlisted_hosts(self):
        self.server.routes["/ok2"] = json_reply({"status": "SUCCESS"})
        self.server.routes["/r2"] = (302, {"Location": "/ok2"}, "", 0)
        good = self.client.execute(make_spec(self.server.url("/r2"), follow_redirects=True, max_redirects=2))
        self.assertEqual(good.response.status_code, 200)
        self.assertEqual(good.response.redirects_followed, 1)
        self.server.routes["/r3"] = (302, {"Location": "https://evil.example.com/x"}, "", 0)
        bad = self.client.execute(make_spec(self.server.url("/r3"), follow_redirects=True, max_redirects=2))
        self.assertEqual(bad.classification, FailureClass.HTTP_ERROR)
        self.assertIn("redirect blocked", bad.detail)

    def test_non_allowlisted_url_never_called(self):
        with mock.patch("requests.Session.request") as req:
            result = self.client.execute(make_spec("http://10.1.1.1/x"))
            req.assert_not_called()
        self.assertEqual(result.classification, FailureClass.CONFIGURATION_ERROR)
        result = self.client.execute(make_spec(self.server.url("/fund-transfer")))
        self.assertEqual(result.classification, FailureClass.CONFIGURATION_ERROR)

    def test_disallowed_method(self):
        result = self.client.execute(make_spec(self.server.url("/ok"), method="DELETE"))
        self.assertEqual(result.classification, FailureClass.CONFIGURATION_ERROR)

    def test_missing_secret_is_configuration_error(self):
        os.environ.pop("T_MISSING", None)
        result = self.client.execute(make_spec(self.server.url("/ok"),
                                               auth={"type": "bearer", "token": "${env:T_MISSING}"}))
        self.assertEqual(result.classification, FailureClass.CONFIGURATION_ERROR)
        self.assertIn("T_MISSING", result.detail)

    def test_environment_proxies_ignored(self):
        self.server.routes["/p"] = json_reply({})
        with mock.patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "http_proxy": "http://127.0.0.1:1"}):
            result = self.client.execute(make_spec(self.server.url("/p")))
        self.assertIsNone(result.classification)


class ClassifierTests(unittest.TestCase):
    def test_classification_table(self):
        cases = [
            (requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED"), FailureClass.TLS_ERROR),
            (requests.exceptions.ConnectTimeout(), FailureClass.TIMEOUT),
            (requests.exceptions.ReadTimeout(), FailureClass.TIMEOUT),
            (requests.exceptions.ConnectionError(ConnectionRefusedError(111, "Connection refused")),
             FailureClass.CONNECTION_ERROR),
            (requests.exceptions.ConnectionError("Failed to resolve 'x' (Name or service not known)"),
             FailureClass.DNS_ERROR),
            (requests.exceptions.InvalidURL("bad"), FailureClass.CONFIGURATION_ERROR),
            (requests.exceptions.ChunkedEncodingError("x"), FailureClass.CONNECTION_ERROR),
        ]
        for exc, expected in cases:
            self.assertEqual(classify_exception(exc)[0], expected, exc)

    def test_detail_never_contains_url(self):
        exc = requests.exceptions.ConnectionError(
            "HTTPConnectionPool(host='h', port=80): Max retries exceeded with url: /acct?no=12345678 "
            "(Caused by NewConnectionError('Connection refused'))")
        cls, detail = classify_exception(exc)
        self.assertEqual(cls, FailureClass.CONNECTION_ERROR)
        self.assertNotIn("12345678", detail)


if __name__ == "__main__":
    unittest.main()
