"""Response validation engine.

An HTTP 200 is never treated as sufficient evidence of availability. A
response is validated in this order:

1. HTTP status code against ``expected_status``          -> HTTP_ERROR
2. Content type against ``content_type``                 -> RESPONSE_VALIDATION_FAILED
3. Body parsing (``body_format``: json | xml | text)    -> INVALID_RESPONSE
4. Rule tree (all / any / not + leaf rules)              -> RESPONSE_VALIDATION_FAILED
5. Response time (warning / critical thresholds)         -> RESPONSE_TIME_EXCEEDED

Leaf rule types
---------------
json_exists, json_not_exists, json_equals, json_not_equals, json_in,
json_regex, json_min_items, xml_exists, xml_not_exists, xml_equals, xml_in,
xml_regex, soap_no_fault, header_equals, header_regex, body_contains.

JSON paths use a safe JSONPath subset: ``$.a.b``, ``$.items[0].id``,
``$.items[*].status``, ``$['key with space']``. XML paths use the
ElementTree XPath subset with a configurable namespace map.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from defusedxml import ElementTree as SafeET  # safe XML parsing (no DTD / entities / external refs)
from defusedxml.common import DefusedXmlException

from logging_config import mask_text
from models import FailureClass, HttpResponseData, Outcome

MAX_REGEX_INPUT = 4096
MAX_ACTUAL_LEN = 120
SOAP_ENV_NAMESPACES = (
    "http://schemas.xmlsoap.org/soap/envelope/",
    "http://www.w3.org/2003/05/soap-envelope",
)
_MISSING = object()


class RuleConfigError(ValueError):
    """Raised when a validation rule is invalid."""


# --------------------------------------------------------------------------- JSON path

_PATH_TOKEN_RE = re.compile(
    r"""\.(?P<key>[A-Za-z_][A-Za-z0-9_\-]*)
      |\.(?P<dotwild>\*)
      |\[(?P<index>-?\d+)\]
      |\[(?P<wild>\*)\]
      |\[(?P<q>['"])(?P<qkey>[^'"]+)(?P=q)\]""",
    re.VERBOSE,
)


def parse_json_path(path: str) -> List[Tuple[str, Any]]:
    if not isinstance(path, str) or not path.startswith("$"):
        raise RuleConfigError(f"JSON path must start with '$': {path!r}")
    tokens: List[Tuple[str, Any]] = []
    pos = 1
    while pos < len(path):
        match = _PATH_TOKEN_RE.match(path, pos)
        if not match:
            raise RuleConfigError(f"invalid JSON path {path!r} at position {pos}")
        if match.group("key") is not None:
            tokens.append(("key", match.group("key")))
        elif match.group("qkey") is not None:
            tokens.append(("key", match.group("qkey")))
        elif match.group("index") is not None:
            tokens.append(("index", int(match.group("index"))))
        else:
            tokens.append(("wild", None))
        pos = match.end()
    return tokens


def resolve_json_path(document: Any, tokens: Sequence[Tuple[str, Any]]) -> List[Any]:
    current: List[Any] = [document]
    for kind, arg in tokens:
        nxt: List[Any] = []
        for node in current:
            if kind == "key" and isinstance(node, dict) and arg in node:
                nxt.append(node[arg])
            elif kind == "index" and isinstance(node, list) and -len(node) <= arg < len(node):
                nxt.append(node[arg])
            elif kind == "wild":
                if isinstance(node, list):
                    nxt.extend(node)
                elif isinstance(node, dict):
                    nxt.extend(node.values())
        current = nxt
        if not current:
            break
    return current


# --------------------------------------------------------------------------- helpers

def sanitize_actual(value: Any, sensitive: bool = False) -> str:
    """Render an observed value for reports/alerts without exposing sensitive data."""
    if sensitive:
        return "<redacted>"
    if value is _MISSING:
        return "<missing>"
    if isinstance(value, (dict, list)):
        text = f"<{type(value).__name__} of {len(value)} item(s)>"
    elif isinstance(value, str):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = json.dumps(value)
    text = mask_text(text)
    return text if len(text) <= MAX_ACTUAL_LEN else text[: MAX_ACTUAL_LEN - 3] + "..."


def _strict_equal(left: Any, right: Any) -> bool:
    """Equality that does not treat True == 1 or "1" == 1."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    return type(left) is type(right) and left == right


@dataclass
class RuleResult:
    passed: bool
    expected: str
    actual: str


@dataclass
class ParsedBody:
    json_doc: Any = _MISSING
    xml_root: Any = None
    text: str = ""
    headers: Dict[str, str] = field(default_factory=dict)


Evaluator = Callable[[ParsedBody], RuleResult]


@dataclass
class RuleNode:
    kind: str  # "all" | "any" | "not" | "leaf"
    description: str = ""
    children: List["RuleNode"] = field(default_factory=list)
    evaluator: Optional[Evaluator] = None
    needs: str = "none"  # json | xml | none

    def evaluate(self, body: ParsedBody) -> Tuple[bool, List[RuleResult]]:
        if self.kind == "leaf":
            assert self.evaluator is not None
            result = self.evaluator(body)
            if self.description:
                result.expected = f"{self.description} ({result.expected})"
            return result.passed, ([] if result.passed else [result])
        if self.kind == "not":
            passed, _ = self.children[0].evaluate(body)
            if passed:
                return False, [RuleResult(False, f"NOT {self.description or 'inner rule'}", "inner rule matched")]
            return True, []
        failures: List[RuleResult] = []
        if self.kind == "all":
            for child in self.children:
                ok, child_failures = child.evaluate(body)
                if not ok:
                    failures.extend(child_failures)
            return not failures, failures
        # any
        for child in self.children:
            ok, child_failures = child.evaluate(body)
            if ok:
                return True, []
            failures.extend(child_failures)
        expected = "ANY OF: " + "; ".join(f.expected for f in failures)
        actual = "; ".join(f.actual for f in failures)
        return False, [RuleResult(False, expected, actual)]

    def requirements(self) -> set:
        reqs = {self.needs} if self.needs != "none" else set()
        for child in self.children:
            reqs |= child.requirements()
        return reqs


# --------------------------------------------------------------------------- leaf factories

def _json_leaf(rule: Dict[str, Any]) -> RuleNode:
    rtype = rule["type"]
    path = rule.get("path")
    if not path:
        raise RuleConfigError(f"rule '{rtype}' requires 'path'")
    tokens = parse_json_path(path)
    sensitive = bool(rule.get("sensitive", False))
    quantifier = rule.get("quantifier", "all")
    if quantifier not in ("all", "any"):
        raise RuleConfigError("quantifier must be 'all' or 'any'")
    pick = all if quantifier == "all" else any

    def values(body: ParsedBody) -> List[Any]:
        return resolve_json_path(body.json_doc, tokens)

    def actual_of(found: List[Any]) -> str:
        if not found:
            return f"{path}=<missing>"
        shown = ", ".join(sanitize_actual(v, sensitive) for v in found[:3])
        more = f" (+{len(found) - 3} more)" if len(found) > 3 else ""
        return f"{path}={shown}{more}"

    if rtype == "json_exists":
        def ev(body: ParsedBody) -> RuleResult:
            found = values(body)
            return RuleResult(bool(found), f"{path} exists", actual_of(found))
    elif rtype == "json_not_exists":
        def ev(body: ParsedBody) -> RuleResult:
            found = values(body)
            return RuleResult(not found, f"{path} absent", actual_of(found))
    elif rtype in ("json_equals", "json_not_equals"):
        if "value" not in rule:
            raise RuleConfigError(f"rule '{rtype}' requires 'value'")
        expected_value = rule["value"]
        negate = rtype == "json_not_equals"
        label = f"{path} {'!=' if negate else '=='} {sanitize_actual(expected_value, sensitive)}"

        def ev(body: ParsedBody) -> RuleResult:
            found = values(body)
            if not found:
                return RuleResult(negate, label, actual_of(found))
            ok = pick((not _strict_equal(v, expected_value)) if negate else _strict_equal(v, expected_value) for v in found)
            return RuleResult(ok, label, actual_of(found))
    elif rtype == "json_in":
        allowed = rule.get("values")
        if not isinstance(allowed, list) or not allowed:
            raise RuleConfigError("rule 'json_in' requires a non-empty 'values' list")
        label = f"{path} in [{', '.join(sanitize_actual(v, sensitive) for v in allowed[:10])}]"

        def ev(body: ParsedBody) -> RuleResult:
            found = values(body)
            ok = bool(found) and pick(any(_strict_equal(v, a) for a in allowed) for v in found)
            return RuleResult(ok, label, actual_of(found))
    elif rtype == "json_regex":
        regex = _compile_regex(rule)
        label = f"{path} matches /{regex.pattern}/"

        def ev(body: ParsedBody) -> RuleResult:
            found = values(body)
            ok = bool(found) and pick(
                isinstance(v, (str, int, float)) and not isinstance(v, bool)
                and regex.search(str(v)[:MAX_REGEX_INPUT]) is not None
                for v in found
            )
            return RuleResult(ok, label, actual_of(found))
    elif rtype == "json_min_items":
        minimum = rule.get("min")
        if not isinstance(minimum, int) or minimum < 0:
            raise RuleConfigError("rule 'json_min_items' requires integer 'min' >= 0")
        label = f"{path} has >= {minimum} item(s)"

        def ev(body: ParsedBody) -> RuleResult:
            found = values(body)
            ok = bool(found) and isinstance(found[0], (list, dict)) and len(found[0]) >= minimum
            return RuleResult(ok, label, actual_of(found))
    else:  # pragma: no cover - guarded by caller
        raise RuleConfigError(f"unknown rule type {rtype!r}")
    return RuleNode("leaf", rule.get("description", ""), evaluator=ev, needs="json")


def _xml_leaf(rule: Dict[str, Any], namespaces: Dict[str, str]) -> RuleNode:
    rtype = rule["type"]
    sensitive = bool(rule.get("sensitive", False))
    if rtype == "soap_no_fault":
        def ev_fault(body: ParsedBody) -> RuleResult:
            fault = _find_soap_fault(body.xml_root)
            if fault is None:
                return RuleResult(True, "no SOAP Fault", "no SOAP Fault")
            code = _local_child_text(fault, ("faultcode", "Value"))
            reason = _local_child_text(fault, ("faultstring", "Text"))
            actual = f"SOAP Fault code={sanitize_actual(code)} reason={sanitize_actual(reason)}"
            return RuleResult(False, "no SOAP Fault", actual)
        return RuleNode("leaf", rule.get("description", ""), evaluator=ev_fault, needs="xml")

    xpath = rule.get("xpath")
    if not xpath:
        raise RuleConfigError(f"rule '{rtype}' requires 'xpath'")
    if xpath.startswith("/"):
        xpath = "." + xpath  # ElementTree does not support absolute paths
    attribute = rule.get("attribute")

    def values(body: ParsedBody) -> List[Any]:
        try:
            elements = body.xml_root.findall(xpath, namespaces)
        except SyntaxError:
            return []
        if attribute:
            return [el.get(attribute) for el in elements if el.get(attribute) is not None]
        return [(el.text or "").strip() for el in elements]

    def actual_of(found: List[Any]) -> str:
        if not found:
            return f"{xpath}=<missing>"
        return f"{xpath}=" + ", ".join(sanitize_actual(v, sensitive) for v in found[:3])

    if rtype == "xml_exists":
        def ev(body: ParsedBody) -> RuleResult:
            found = values(body)
            return RuleResult(bool(found), f"{xpath} exists", actual_of(found))
    elif rtype == "xml_not_exists":
        def ev(body: ParsedBody) -> RuleResult:
            found = values(body)
            return RuleResult(not found, f"{xpath} absent", actual_of(found))
    elif rtype == "xml_equals":
        if "value" not in rule:
            raise RuleConfigError("rule 'xml_equals' requires 'value'")
        expected_value = str(rule["value"])

        def ev(body: ParsedBody) -> RuleResult:
            found = values(body)
            ok = bool(found) and all(v == expected_value for v in found)
            return RuleResult(ok, f"{xpath} == {sanitize_actual(expected_value, sensitive)}", actual_of(found))
    elif rtype == "xml_in":
        allowed = rule.get("values")
        if not isinstance(allowed, list) or not allowed:
            raise RuleConfigError("rule 'xml_in' requires a non-empty 'values' list")
        allowed_s = [str(a) for a in allowed]

        def ev(body: ParsedBody) -> RuleResult:
            found = values(body)
            ok = bool(found) and all(v in allowed_s for v in found)
            return RuleResult(ok, f"{xpath} in {allowed_s[:10]}", actual_of(found))
    elif rtype == "xml_regex":
        regex = _compile_regex(rule)

        def ev(body: ParsedBody) -> RuleResult:
            found = values(body)
            ok = bool(found) and all(regex.search(v[:MAX_REGEX_INPUT]) is not None for v in found)
            return RuleResult(ok, f"{xpath} matches /{regex.pattern}/", actual_of(found))
    else:  # pragma: no cover
        raise RuleConfigError(f"unknown rule type {rtype!r}")
    return RuleNode("leaf", rule.get("description", ""), evaluator=ev, needs="xml")


def _other_leaf(rule: Dict[str, Any]) -> RuleNode:
    rtype = rule["type"]
    if rtype in ("header_equals", "header_regex"):
        name = rule.get("name")
        if not name:
            raise RuleConfigError(f"rule '{rtype}' requires 'name'")
        lname = name.lower()

        def header(body: ParsedBody) -> Optional[str]:
            for key, val in body.headers.items():
                if key.lower() == lname:
                    return val
            return None

        if rtype == "header_equals":
            expected_value = str(rule.get("value", ""))

            def ev(body: ParsedBody) -> RuleResult:
                val = header(body)
                return RuleResult(val == expected_value, f"header {name} == {expected_value!r}",
                                  f"header {name}={sanitize_actual(val if val is not None else _MISSING)}")
        else:
            regex = _compile_regex(rule)

            def ev(body: ParsedBody) -> RuleResult:
                val = header(body)
                ok = val is not None and regex.search(val[:MAX_REGEX_INPUT]) is not None
                return RuleResult(ok, f"header {name} matches /{regex.pattern}/",
                                  f"header {name}={sanitize_actual(val if val is not None else _MISSING)}")
        return RuleNode("leaf", rule.get("description", ""), evaluator=ev)
    if rtype == "body_contains":
        needle = rule.get("value")
        if not isinstance(needle, str) or not needle:
            raise RuleConfigError("rule 'body_contains' requires a non-empty string 'value'")
        negate = bool(rule.get("negate", False))

        def ev(body: ParsedBody) -> RuleResult:
            present = needle in body.text
            label = f"body {'does not contain' if negate else 'contains'} {needle!r}"
            return RuleResult(present != negate, label, "present" if present else "absent")
        return RuleNode("leaf", rule.get("description", ""), evaluator=ev)
    raise RuleConfigError(f"unknown rule type {rtype!r}")


def _compile_regex(rule: Dict[str, Any]) -> "re.Pattern[str]":
    pattern = rule.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise RuleConfigError(f"rule '{rule.get('type')}' requires 'pattern'")
    if len(pattern) > 256:
        raise RuleConfigError("regex pattern is too long (max 256 characters)")
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise RuleConfigError(f"invalid regex {pattern!r}: {exc}") from None


JSON_RULES = {"json_exists", "json_not_exists", "json_equals", "json_not_equals", "json_in", "json_regex", "json_min_items"}
XML_RULES = {"xml_exists", "xml_not_exists", "xml_equals", "xml_in", "xml_regex", "soap_no_fault"}
OTHER_RULES = {"header_equals", "header_regex", "body_contains"}
RULE_TYPES = JSON_RULES | XML_RULES | OTHER_RULES


def compile_rule(rule: Any, namespaces: Optional[Dict[str, str]] = None, depth: int = 0) -> RuleNode:
    namespaces = namespaces or {}
    if depth > 10:
        raise RuleConfigError("validation rules are nested too deeply (max 10)")
    if not isinstance(rule, dict):
        raise RuleConfigError("each validation rule must be an object")
    for combinator in ("all", "any"):
        if combinator in rule:
            children = rule[combinator]
            if not isinstance(children, list) or not children:
                raise RuleConfigError(f"'{combinator}' must be a non-empty list")
            return RuleNode(combinator, rule.get("description", ""),
                            children=[compile_rule(c, namespaces, depth + 1) for c in children])
    if "not" in rule:
        return RuleNode("not", rule.get("description", ""), children=[compile_rule(rule["not"], namespaces, depth + 1)])
    rtype = rule.get("type")
    if rtype in JSON_RULES:
        return _json_leaf(rule)
    if rtype in XML_RULES:
        return _xml_leaf(rule, namespaces)
    if rtype in OTHER_RULES:
        return _other_leaf(rule)
    raise RuleConfigError(f"unknown rule type {rtype!r}")


# --------------------------------------------------------------------------- validation spec

@dataclass
class ValidationSpec:
    expected_status: List[int]
    content_type: Optional[str]
    body_format: str  # json | xml | text | none
    rules: Optional[RuleNode]
    warning_ms: Optional[float]
    critical_ms: Optional[float]

    @classmethod
    def from_config(cls, validation: Dict[str, Any], response_time: Dict[str, Any]) -> "ValidationSpec":
        namespaces = validation.get("namespaces") or {}
        rules_cfg = validation.get("rules")
        rules = compile_rule(rules_cfg, namespaces) if rules_cfg else None
        body_format = validation.get("body_format", "json")
        if rules is not None:
            reqs = rules.requirements()
            if "json" in reqs and body_format != "json":
                raise RuleConfigError("JSON rules require body_format 'json'")
            if "xml" in reqs and body_format != "xml":
                raise RuleConfigError("XML rules require body_format 'xml'")
        warning = response_time.get("warning_ms")
        critical = response_time.get("critical_ms")
        if warning is not None and critical is not None and warning > critical:
            raise RuleConfigError("response_time.warning_ms must be <= critical_ms")
        return cls(
            expected_status=list(validation.get("expected_status") or [200]),
            content_type=validation.get("content_type"),
            body_format=body_format,
            rules=rules,
            warning_ms=warning,
            critical_ms=critical,
        )


@dataclass
class ValidationReport:
    outcome: Outcome
    classification: Optional[FailureClass]
    expected: str
    actual: str
    failures: List[str] = field(default_factory=list)


def _decode(body: bytes, content_type: str) -> str:
    charset = "utf-8"
    match = re.search(r"charset=([\w\-]+)", content_type or "", re.IGNORECASE)
    if match:
        charset = match.group(1)
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _find_soap_fault(root: Any) -> Any:
    if root is None:
        return None
    for element in root.iter():
        tag = element.tag if isinstance(element.tag, str) else ""
        if tag.endswith("}Fault") and tag[1:].split("}")[0] in SOAP_ENV_NAMESPACES:
            return element
    return None


def _local_child_text(element: Any, local_names: Tuple[str, ...]) -> str:
    for child in element.iter():
        tag = child.tag if isinstance(child.tag, str) else ""
        local = tag.split("}")[-1]
        if local in local_names and (child.text or "").strip():
            return (child.text or "").strip()
    return ""


def _status_label(codes: Sequence[int]) -> str:
    return "HTTP " + "/".join(str(c) for c in codes)


def validate_response(response: HttpResponseData, spec: ValidationSpec) -> ValidationReport:
    """Validate a received response. Never raises for malformed responses."""
    expected_status = _status_label(spec.expected_status)
    if response.status_code not in spec.expected_status:
        return ValidationReport(Outcome.FAILURE, FailureClass.HTTP_ERROR, expected_status,
                                f"HTTP {response.status_code}",
                                [f"unexpected HTTP status {response.status_code}"])

    if spec.content_type:
        actual_ct = response.content_type.split(";")[0].strip().lower()
        if actual_ct != spec.content_type.lower():
            return ValidationReport(Outcome.FAILURE, FailureClass.RESPONSE_VALIDATION_FAILED,
                                    f"Content-Type {spec.content_type}",
                                    f"Content-Type {sanitize_actual(actual_ct or '<none>')}",
                                    ["unexpected content type"])

    parsed = ParsedBody(headers=response.headers)
    if spec.body_format != "none":
        parsed.text = _decode(response.body, response.content_type)
    if spec.body_format == "json":
        try:
            parsed.json_doc = json.loads(parsed.text)
        except ValueError:
            return ValidationReport(Outcome.FAILURE, FailureClass.INVALID_RESPONSE, "valid JSON body",
                                    f"unparseable JSON ({len(response.body)} bytes)", ["invalid JSON"])
    elif spec.body_format == "xml":
        try:
            parsed.xml_root = SafeET.fromstring(response.body, forbid_dtd=True,
                                                forbid_entities=True, forbid_external=True)
        except DefusedXmlException:
            return ValidationReport(Outcome.FAILURE, FailureClass.INVALID_RESPONSE, "safe XML body",
                                    "XML rejected by safe parser (DTD/entity)", ["unsafe XML"])
        except SafeET.ParseError:
            return ValidationReport(Outcome.FAILURE, FailureClass.INVALID_RESPONSE, "valid XML body",
                                    f"unparseable XML ({len(response.body)} bytes)", ["invalid XML"])

    if spec.rules is not None:
        try:
            passed, failures = spec.rules.evaluate(parsed)
        except Exception as exc:  # defensive: unexpected structure must not crash the check
            return ValidationReport(Outcome.FAILURE, FailureClass.RESPONSE_VALIDATION_FAILED,
                                    "rules evaluable", f"rule evaluation error ({exc.__class__.__name__})",
                                    ["rule evaluation error"])
        if not passed:
            return ValidationReport(
                Outcome.FAILURE, FailureClass.RESPONSE_VALIDATION_FAILED,
                "; ".join(f.expected for f in failures)[:500],
                "; ".join(f.actual for f in failures)[:500],
                [f"{f.expected} | actual {f.actual}" for f in failures][:10],
            )

    return evaluate_response_time(response.elapsed_ms, spec, expected_status)


def evaluate_response_time(elapsed_ms: float, spec: ValidationSpec, expected: str = "") -> ValidationReport:
    actual = f"OK in {elapsed_ms:.0f} ms"
    if spec.critical_ms is not None and elapsed_ms > spec.critical_ms:
        return ValidationReport(Outcome.FAILURE, FailureClass.RESPONSE_TIME_EXCEEDED,
                                f"response time <= {spec.critical_ms:.0f} ms (critical)",
                                f"{elapsed_ms:.0f} ms", ["critical response time exceeded"])
    if spec.warning_ms is not None and elapsed_ms > spec.warning_ms:
        return ValidationReport(Outcome.DEGRADED, FailureClass.RESPONSE_TIME_EXCEEDED,
                                f"response time <= {spec.warning_ms:.0f} ms (warning)",
                                f"{elapsed_ms:.0f} ms", ["warning response time exceeded"])
    return ValidationReport(Outcome.SUCCESS, None, expected or "valid response", actual)
