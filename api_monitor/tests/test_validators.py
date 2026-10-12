"""Response validation engine tests."""
import json
import unittest

from tests.helpers import ROOT  # noqa: F401  (sets sys.path)

from models import FailureClass, HttpResponseData, Outcome
from validators import (RuleConfigError, ValidationSpec, compile_rule, parse_json_path, resolve_json_path,
                        validate_response)


def resp(body, status=200, ctype="application/json", elapsed=100.0, headers=None):
    data = body if isinstance(body, bytes) else (json.dumps(body) if not isinstance(body, str) else body).encode()
    hdrs = {"Content-Type": ctype}
    hdrs.update(headers or {})
    return HttpResponseData(status_code=status, headers=hdrs, body=data, elapsed_ms=elapsed)


def spec(rules=None, body_format="json", content_type="application/json", expected=(200,),
         warning=None, critical=None, namespaces=None):
    validation = {"expected_status": list(expected), "content_type": content_type, "body_format": body_format}
    if rules is not None:
        validation["rules"] = rules
    if namespaces:
        validation["namespaces"] = namespaces
    return ValidationSpec.from_config(validation, {"warning_ms": warning, "critical_ms": critical})


class JsonPathTests(unittest.TestCase):
    DOC = {"a": {"b": [{"id": 1, "s": "X"}, {"id": 2, "s": "Y"}]}, "key with space": 5}

    def test_paths(self):
        self.assertEqual(resolve_json_path(self.DOC, parse_json_path("$.a.b[0].id")), [1])
        self.assertEqual(resolve_json_path(self.DOC, parse_json_path("$.a.b[-1].s")), ["Y"])
        self.assertEqual(resolve_json_path(self.DOC, parse_json_path("$.a.b[*].s")), ["X", "Y"])
        self.assertEqual(resolve_json_path(self.DOC, parse_json_path("$['key with space']")), [5])
        self.assertEqual(resolve_json_path(self.DOC, parse_json_path("$.missing.x")), [])
        self.assertEqual(resolve_json_path(self.DOC, parse_json_path("$.a.b[5]")), [])
        self.assertEqual(resolve_json_path([1, 2], parse_json_path("$.a")), [])

    def test_invalid_paths(self):
        for bad in ("a.b", "$..a", "$.a[", "$.a b"):
            with self.assertRaises(RuleConfigError):
                parse_json_path(bad)


class ValidationTests(unittest.TestCase):
    def test_http_200_with_business_error_fails(self):
        s = spec({"type": "json_equals", "path": "$.status", "value": "SUCCESS"})
        report = validate_response(resp({"status": "FAILED", "errorCode": "CORE_TIMEOUT"}), s)
        self.assertEqual(report.outcome, Outcome.FAILURE)
        self.assertEqual(report.classification, FailureClass.RESPONSE_VALIDATION_FAILED)
        self.assertIn('"FAILED"', report.actual)
        self.assertIn("SUCCESS", report.expected)

    def test_error_code_must_be_absent(self):
        s = spec({"type": "json_not_exists", "path": "$.errorCode"})
        self.assertEqual(validate_response(resp({"status": "OK"}), s).outcome, Outcome.SUCCESS)
        report = validate_response(resp({"status": "OK", "errorCode": "CORE_TIMEOUT"}), s)
        self.assertEqual(report.outcome, Outcome.FAILURE)
        self.assertIn("CORE_TIMEOUT", report.actual)

    def test_success(self):
        s = spec({"all": [{"type": "json_equals", "path": "$.status", "value": "SUCCESS"},
                          {"type": "json_exists", "path": "$.data.balance"}]})
        report = validate_response(resp({"status": "SUCCESS", "data": {"balance": 10}}), s)
        self.assertEqual(report.outcome, Outcome.SUCCESS)
        self.assertIsNone(report.classification)

    def test_http_status_errors(self):
        s = spec({"type": "json_exists", "path": "$.x"})
        for code in (500, 503, 404):
            report = validate_response(resp({"x": 1}, status=code), s)
            self.assertEqual(report.classification, FailureClass.HTTP_ERROR)
            self.assertEqual(report.actual, f"HTTP {code}")

    def test_content_type(self):
        s = spec(None)
        report = validate_response(resp("<html/>", ctype="text/html"), s)
        self.assertEqual(report.classification, FailureClass.RESPONSE_VALIDATION_FAILED)
        ok = validate_response(resp({"a": 1}, ctype="application/json; charset=utf-8"), s)
        self.assertEqual(ok.outcome, Outcome.SUCCESS)

    def test_invalid_json(self):
        report = validate_response(resp("{not json"), spec(None))
        self.assertEqual(report.classification, FailureClass.INVALID_RESPONSE)

    def test_unexpected_structure(self):
        s = spec({"type": "json_equals", "path": "$.result.status", "value": "OK"})
        for body in ([1, 2, 3], {"result": "OK"}, {"result": {"status": None}}, "\"just a string\""):
            report = validate_response(resp(body), s)
            self.assertEqual(report.classification, FailureClass.RESPONSE_VALIDATION_FAILED, body)

    def test_json_in_and_regex(self):
        s = spec({"all": [{"type": "json_in", "path": "$.st", "values": ["ACTIVE", "DORMANT"]},
                          {"type": "json_regex", "path": "$.ccy", "pattern": "^[A-Z]{3}$"}]})
        self.assertEqual(validate_response(resp({"st": "DORMANT", "ccy": "SAR"}), s).outcome, Outcome.SUCCESS)
        report = validate_response(resp({"st": "CLOSED", "ccy": "sar"}), s)
        self.assertEqual(report.outcome, Outcome.FAILURE)
        self.assertEqual(report.expected.count(";"), 1)  # both failures reported

    def test_any_not_combinations(self):
        rules = {"all": [
            {"any": [{"type": "json_equals", "path": "$.code", "value": "0000"},
                     {"type": "json_equals", "path": "$.result.status", "value": "OK"}]},
            {"not": {"type": "json_in", "path": "$.status", "values": ["CLOSED"]}},
        ]}
        s = spec(rules)
        self.assertEqual(validate_response(resp({"code": "0000"}), s).outcome, Outcome.SUCCESS)
        self.assertEqual(validate_response(resp({"result": {"status": "OK"}}), s).outcome, Outcome.SUCCESS)
        self.assertEqual(validate_response(resp({"code": "9999"}), s).outcome, Outcome.FAILURE)
        self.assertEqual(validate_response(resp({"code": "0000", "status": "CLOSED"}), s).outcome, Outcome.FAILURE)

    def test_strict_equality(self):
        s = spec({"type": "json_equals", "path": "$.v", "value": 1})
        self.assertEqual(validate_response(resp({"v": 1}), s).outcome, Outcome.SUCCESS)
        self.assertEqual(validate_response(resp({"v": 1.0}), s).outcome, Outcome.SUCCESS)
        self.assertEqual(validate_response(resp({"v": True}), s).outcome, Outcome.FAILURE)
        self.assertEqual(validate_response(resp({"v": "1"}), s).outcome, Outcome.FAILURE)

    def test_wildcard_quantifiers(self):
        doc = {"items": [{"s": "OK"}, {"s": "ERR"}]}
        all_spec = spec({"type": "json_equals", "path": "$.items[*].s", "value": "OK"})
        any_spec = spec({"type": "json_equals", "path": "$.items[*].s", "value": "OK", "quantifier": "any"})
        self.assertEqual(validate_response(resp(doc), all_spec).outcome, Outcome.FAILURE)
        self.assertEqual(validate_response(resp(doc), any_spec).outcome, Outcome.SUCCESS)
        min_spec = spec({"type": "json_min_items", "path": "$.items", "min": 3})
        self.assertEqual(validate_response(resp(doc), min_spec).outcome, Outcome.FAILURE)

    def test_sensitive_values_are_not_reported(self):
        s = spec({"type": "json_equals", "path": "$.acct", "value": "X", "sensitive": True})
        report = validate_response(resp({"acct": "SA0380000000608010167519"}), s)
        self.assertIn("<redacted>", report.actual)
        self.assertNotIn("608010167519", report.actual)

    def test_long_numbers_are_masked(self):
        s = spec({"type": "json_equals", "path": "$.acct", "value": "X"})
        report = validate_response(resp({"acct": "1234567890123456"}), s)
        self.assertNotIn("1234567890123456", report.actual)
        self.assertIn("3456", report.actual)

    def test_headers_and_body_contains(self):
        s = spec({"all": [{"type": "header_regex", "name": "X-Backend", "pattern": "^core-"},
                          {"type": "body_contains", "value": "ERROR", "negate": True}]}, body_format="text",
                 content_type=None)
        good = resp("all fine", ctype="text/plain", headers={"X-Backend": "core-01"})
        bad = resp("ERROR in core", ctype="text/plain", headers={"X-Backend": "core-01"})
        self.assertEqual(validate_response(good, s).outcome, Outcome.SUCCESS)
        self.assertEqual(validate_response(bad, s).outcome, Outcome.FAILURE)

    def test_response_time_thresholds(self):
        s = spec(None, warning=500, critical=2000)
        self.assertEqual(validate_response(resp({}, elapsed=100), s).outcome, Outcome.SUCCESS)
        degraded = validate_response(resp({}, elapsed=800), s)
        self.assertEqual(degraded.outcome, Outcome.DEGRADED)
        self.assertEqual(degraded.classification, FailureClass.RESPONSE_TIME_EXCEEDED)
        failed = validate_response(resp({}, elapsed=2500), s)
        self.assertEqual(failed.outcome, Outcome.FAILURE)
        self.assertEqual(failed.classification, FailureClass.RESPONSE_TIME_EXCEEDED)

    def test_invalid_rule_definitions(self):
        for rule in ({"type": "json_equals", "path": "$.a"}, {"type": "json_in", "path": "$.a", "values": []},
                     {"type": "nope"}, {"all": []}, {"type": "xml_exists"}, "string"):
            with self.assertRaises(RuleConfigError):
                compile_rule(rule)


SOAP_OK = """<?xml version="1.0"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:c="urn:card">
 <soapenv:Body><c:Resp><c:Result><c:Code>00</c:Code></c:Result><c:CardStatus>ACTIVE</c:CardStatus>
 <c:Expiry when="2030-01"/></c:Resp></soapenv:Body></soapenv:Envelope>"""
SOAP_FAULT = """<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
 <soapenv:Body><soapenv:Fault><faultcode>soapenv:Server</faultcode><faultstring>Backend unavailable</faultstring>
 </soapenv:Fault></soapenv:Body></soapenv:Envelope>"""
NS = {"soapenv": "http://schemas.xmlsoap.org/soap/envelope/", "c": "urn:card"}


class XmlValidationTests(unittest.TestCase):
    def _spec(self, rules):
        return spec(rules, body_format="xml", content_type="text/xml", namespaces=NS)

    def test_soap_success(self):
        s = self._spec({"all": [{"type": "soap_no_fault"},
                                {"type": "xml_equals", "xpath": "./soapenv:Body/c:Resp/c:Result/c:Code", "value": "00"},
                                {"type": "xml_in", "xpath": ".//c:CardStatus", "values": ["ACTIVE"]},
                                {"type": "xml_regex", "xpath": ".//c:Expiry", "attribute": "when",
                                 "pattern": "^\\d{4}-\\d{2}$"}]})
        report = validate_response(resp(SOAP_OK, ctype="text/xml"), s)
        self.assertEqual(report.outcome, Outcome.SUCCESS, report.actual)

    def test_soap_fault_detected(self):
        s = self._spec({"type": "soap_no_fault"})
        report = validate_response(resp(SOAP_FAULT, ctype="text/xml"), s)
        self.assertEqual(report.classification, FailureClass.RESPONSE_VALIDATION_FAILED)
        self.assertIn("Backend unavailable", report.actual)

    def test_wrong_code(self):
        s = self._spec({"type": "xml_equals", "xpath": ".//c:Code", "value": "05"})
        report = validate_response(resp(SOAP_OK, ctype="text/xml"), s)
        self.assertEqual(report.outcome, Outcome.FAILURE)
        self.assertIn('"00"', report.actual)

    def test_invalid_xml(self):
        report = validate_response(resp("<a><b></a>", ctype="text/xml"), self._spec({"type": "soap_no_fault"}))
        self.assertEqual(report.classification, FailureClass.INVALID_RESPONSE)

    def test_xxe_and_entity_expansion_blocked(self):
        xxe = ('<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
               '<r>&x;</r>')
        bomb = ('<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><l>&b;</l>')
        for doc in (xxe, bomb):
            report = validate_response(resp(doc, ctype="text/xml"), self._spec({"type": "xml_exists", "xpath": "."}))
            self.assertEqual(report.classification, FailureClass.INVALID_RESPONSE)


if __name__ == "__main__":
    unittest.main()
