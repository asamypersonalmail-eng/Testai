"""Secure HTTP execution for health checks and the e-mail API.

Safety properties:
* every URL (including redirect targets) is checked against the host allowlist;
* TLS verification is always on (``verify`` is either True or a CA bundle path);
* redirects are not followed unless both the service and the global policy allow it;
* explicit connect/read timeouts plus an overall deadline enforced while streaming;
* the response body is read up to ``max_response_bytes`` only;
* a fresh session per execution, always closed - no connection or thread leaks;
* environment proxies are ignored unless explicitly enabled;
* error details are generic and never include URLs, payloads or credentials.
"""
from __future__ import annotations

import socket
import ssl
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter

from config_loader import HostAllowlist
from models import FailureClass, HttpResponseData
from secrets_resolver import SecretResolutionError, resolve

REDIRECT_CODES = (301, 302, 303, 307, 308)
USER_AGENT = "api-synthetic-monitor/1.0"


@dataclass
class RequestSpec:
    url: str
    method: str
    headers: Dict[str, str]
    query_params: Dict[str, str]
    body: Optional[Dict[str, Any]]
    auth: Dict[str, Any]
    connect_timeout: float
    read_timeout: float
    total_timeout: float
    follow_redirects: bool = False
    max_redirects: int = 0
    ca_bundle: Optional[str] = None
    client_cert: Optional[Dict[str, str]] = None
    url_label: str = ""
    resolve_body: bool = True  # False when the body was already rendered (e-mail payloads)


@dataclass
class AttemptResult:
    response: Optional[HttpResponseData]
    classification: Optional[FailureClass]
    detail: str
    elapsed_ms: float


class PolicyViolation(Exception):
    pass


def _iter_causes(exc: BaseException) -> Iterator[BaseException]:
    seen = set()
    stack = [exc]
    while stack and len(seen) < 20:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        yield cur
        for nxt in (cur.__cause__, cur.__context__, getattr(cur, "reason", None)):
            if isinstance(nxt, BaseException):
                stack.append(nxt)
        for arg in getattr(cur, "args", ()):
            if isinstance(arg, BaseException):
                stack.append(arg)


_DNS_MARKERS = ("name or service not known", "nodename nor servname", "getaddrinfo failed",
                "temporary failure in name resolution", "failed to resolve", "no address associated",
                "name resolution")
_TLS_REASONS = ("CERTIFICATE_VERIFY_FAILED", "WRONG_VERSION_NUMBER", "HOSTNAME_MISMATCH",
                "UNSUPPORTED_PROTOCOL", "TLSV1_ALERT", "SSLV3_ALERT_HANDSHAKE_FAILURE",
                "UNKNOWN_PROTOCOL", "certificate has expired", "self-signed certificate",
                "unable to get local issuer certificate", "Hostname mismatch")


def classify_exception(exc: BaseException) -> Tuple[FailureClass, str]:
    """Map a requests/urllib3/socket exception to a failure classification + safe detail."""
    if isinstance(exc, requests.exceptions.SSLError) or any(isinstance(c, ssl.SSLError) for c in _iter_causes(exc)):
        text = " ".join(str(c) for c in _iter_causes(exc))
        reason = next((r for r in _TLS_REASONS if r.lower() in text.lower()), "handshake failed")
        return FailureClass.TLS_ERROR, f"TLS error: {reason}"
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return FailureClass.TIMEOUT, "connect timeout"
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return FailureClass.TIMEOUT, "read timeout"
    if isinstance(exc, (requests.exceptions.InvalidURL, requests.exceptions.MissingSchema,
                        requests.exceptions.InvalidSchema, requests.exceptions.InvalidHeader)):
        return FailureClass.CONFIGURATION_ERROR, f"invalid request definition ({exc.__class__.__name__})"
    causes = list(_iter_causes(exc))
    for cause in causes:
        name = cause.__class__.__name__
        if isinstance(cause, socket.gaierror) or name == "NameResolutionError":
            return FailureClass.DNS_ERROR, "DNS resolution failed"
    text = " ".join(str(c) for c in causes).lower()
    if any(marker in text for marker in _DNS_MARKERS):
        return FailureClass.DNS_ERROR, "DNS resolution failed"
    for cause in causes:
        if isinstance(cause, ConnectionRefusedError) or "connection refused" in str(cause).lower():
            return FailureClass.CONNECTION_ERROR, "connection refused"
        if isinstance(cause, ConnectionResetError):
            return FailureClass.CONNECTION_ERROR, "connection reset by peer"
        if isinstance(cause, socket.timeout) or cause.__class__.__name__ in ("ConnectTimeoutError", "ReadTimeoutError"):
            return FailureClass.TIMEOUT, "socket timeout"
    if isinstance(exc, requests.exceptions.ChunkedEncodingError):
        return FailureClass.CONNECTION_ERROR, "connection dropped while reading response"
    if isinstance(exc, requests.exceptions.ContentDecodingError):
        return FailureClass.INVALID_RESPONSE, "response content could not be decoded"
    if "remotedisconnected" in text or "connection aborted" in text:
        return FailureClass.CONNECTION_ERROR, "connection closed by remote host"
    return FailureClass.CONNECTION_ERROR, f"connection error ({exc.__class__.__name__})"


def _apply_auth(auth: Dict[str, Any], headers: Dict[str, str]) -> Optional[Tuple[str, str]]:
    kind = auth.get("type", "none")
    if kind == "basic":
        return (auth["username"], auth["password"])
    if kind == "bearer":
        headers["Authorization"] = f"Bearer {auth['token']}"
    elif kind == "api_key":
        headers[auth["header"]] = auth["value"]
    return None


class SecureHttpClient:
    def __init__(self, security: Dict[str, Any], max_response_bytes: int) -> None:
        self.security = security
        self.max_response_bytes = int(max_response_bytes)
        self.allowlist = HostAllowlist(security["allowed_hosts"], security["allowed_schemes"],
                                       security.get("forbidden_url_keywords", []))
        self.allowed_methods = {m.upper() for m in security["allowed_methods"]}

    # ------------------------------------------------------------------ session
    def _new_session(self, spec: RequestSpec) -> requests.Session:
        session = requests.Session()
        session.trust_env = bool(self.security.get("use_environment_proxies", False))
        proxies = self.security.get("proxies") or {}
        if proxies:
            session.proxies.update(proxies)
        adapter = HTTPAdapter(max_retries=0, pool_connections=1, pool_maxsize=1)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.verify = spec.ca_bundle or self.security.get("ca_bundle") or True
        if spec.client_cert:
            session.cert = (spec.client_cert["cert_file"], spec.client_cert["key_file"])
        session.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})
        return session

    # ------------------------------------------------------------------ execute
    def execute(self, spec: RequestSpec, *, extra_methods: Tuple[str, ...] = ()) -> AttemptResult:
        """Execute one HTTP attempt. Never raises; failures are classified."""
        start = time.monotonic()

        def elapsed() -> float:
            return (time.monotonic() - start) * 1000.0

        method = spec.method.upper()
        if method not in self.allowed_methods and method not in extra_methods:
            return AttemptResult(None, FailureClass.CONFIGURATION_ERROR, f"method {method} not allowed", 0.0)
        reason = self.allowlist.check(spec.url)
        if reason:
            return AttemptResult(None, FailureClass.CONFIGURATION_ERROR, f"blocked by allowlist: {reason}", 0.0)

        try:
            headers = resolve(dict(spec.headers))
            params = resolve(dict(spec.query_params))
            auth_cfg = resolve(dict(spec.auth))
            body = (resolve(spec.body) if spec.resolve_body else spec.body) if spec.body else None
        except SecretResolutionError as exc:
            return AttemptResult(None, FailureClass.CONFIGURATION_ERROR, f"secret resolution failed: {exc}", 0.0)

        basic = _apply_auth(auth_cfg, headers)
        kwargs: Dict[str, Any] = {"headers": headers, "params": params or None, "auth": basic}
        if body is not None:
            if body["type"] == "json":
                kwargs["json"] = body["content"]
            else:
                content = body["content"] if isinstance(body["content"], str) else str(body["content"])
                kwargs["data"] = content.encode("utf-8")
                if body.get("content_type"):
                    headers.setdefault("Content-Type", body["content_type"])

        session = self._new_session(spec)
        url = spec.url
        redirects = 0
        try:
            while True:
                remaining = spec.total_timeout - (time.monotonic() - start)
                if remaining <= 0:
                    return AttemptResult(None, FailureClass.TIMEOUT, "total deadline exceeded", elapsed())
                timeout = (min(spec.connect_timeout, remaining), min(spec.read_timeout, remaining))
                try:
                    response = session.request(method, url, timeout=timeout, allow_redirects=False,
                                               stream=True, **kwargs)
                except requests.exceptions.RequestException as exc:
                    cls, detail = classify_exception(exc)
                    return AttemptResult(None, cls, detail, elapsed())
                except (OSError, ValueError) as exc:  # e.g. missing CA bundle / cert file
                    cls, detail = classify_exception(exc)
                    if isinstance(exc, (FileNotFoundError, IsADirectoryError)):
                        cls, detail = FailureClass.CONFIGURATION_ERROR, "TLS material (CA bundle / client certificate) not readable"
                    return AttemptResult(None, cls, detail, elapsed())

                try:
                    if response.status_code in REDIRECT_CODES and spec.follow_redirects \
                            and self.security.get("allow_redirects", False):
                        if redirects >= spec.max_redirects:
                            return AttemptResult(None, FailureClass.HTTP_ERROR, "too many redirects", elapsed())
                        location = response.headers.get("Location", "")
                        target = urljoin(url, location)
                        why = self.allowlist.check(target)
                        if why:
                            return AttemptResult(None, FailureClass.HTTP_ERROR,
                                                 f"redirect blocked: {why}", elapsed())
                        if urlsplit(target).hostname != urlsplit(url).hostname:
                            headers.pop("Authorization", None)
                            kwargs["auth"] = None
                        if response.status_code in (301, 302, 303) and method != "HEAD":
                            method = "GET"
                            kwargs.pop("json", None)
                            kwargs.pop("data", None)
                        url = target
                        redirects += 1
                        continue
                    return self._read_body(response, spec, start, redirects)
                finally:
                    response.close()
        except Exception as exc:  # defensive: never let one check crash the worker
            cls, detail = classify_exception(exc)
            return AttemptResult(None, cls, detail, elapsed())
        finally:
            session.close()

    def _read_body(self, response: requests.Response, spec: RequestSpec, start: float, redirects: int) -> AttemptResult:
        def elapsed() -> float:
            return (time.monotonic() - start) * 1000.0

        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > self.max_response_bytes:
            return AttemptResult(None, FailureClass.RESPONSE_TOO_LARGE,
                                 f"declared Content-Length {declared} exceeds limit", elapsed())
        chunks = []
        size = 0
        try:
            for chunk in response.iter_content(chunk_size=8192):
                size += len(chunk)
                if size > self.max_response_bytes:
                    return AttemptResult(None, FailureClass.RESPONSE_TOO_LARGE,
                                         f"response exceeds {self.max_response_bytes} bytes", elapsed())
                if time.monotonic() - start > spec.total_timeout:
                    return AttemptResult(None, FailureClass.TIMEOUT, "total deadline exceeded while reading", elapsed())
                chunks.append(chunk)
        except requests.exceptions.RequestException as exc:
            cls, detail = classify_exception(exc)
            return AttemptResult(None, cls, detail, elapsed())
        data = HttpResponseData(
            status_code=response.status_code,
            headers={k: v for k, v in response.headers.items()},
            body=b"".join(chunks),
            elapsed_ms=elapsed(),
            url_label=spec.url_label,
            redirects_followed=redirects,
        )
        return AttemptResult(data, None, "", data.elapsed_ms)
