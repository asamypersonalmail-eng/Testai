"""Configuration loading and validation tests."""
import os
import tempfile
import unittest

from tests.helpers import ROOT, make_raw_config, make_service, write_config

from config_loader import ConfigError, HostAllowlist, load_config, parse_config, sanitize_endpoint


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _raw(self, **svc_overrides):
        return make_raw_config(self.dir, [make_service("svc-a", "http://127.0.0.1:8080/ok", **svc_overrides)])

    def assertConfigError(self, raw, fragment):
        with self.assertRaises(ConfigError) as ctx:
            parse_config(raw)
        joined = "\n".join(ctx.exception.errors)
        self.assertIn(fragment, joined)

    def test_sample_config_is_valid(self):
        config = load_config(os.path.join(ROOT, "config.json"))
        self.assertEqual(config.environment, "PROD")
        intervals = {s.id: s.interval_seconds for s in config.services}
        self.assertEqual(intervals["customer-account-inquiry"], 3600)
        self.assertEqual(intervals["card-details-inquiry"], 1800)
        self.assertEqual(intervals["customer-profile-inquiry"], 900)
        # each service has distinct validation / alert policy
        self.assertEqual(intervals["get-position"], 900)
        self.assertGreaterEqual(len({s.alerting.severity for s in config.services}), 3)
        self.assertGreaterEqual(len({s.validation.warning_ms for s in config.services}), 3)

    def test_defaults_are_merged_and_units_converted(self):
        raw = self._raw(interval={"value": 2, "unit": "minutes"})
        del raw["services"][0]["alerting"]
        raw["defaults"] = {"alerting": {"failure_threshold": 4, "severity": "LOW"}}
        config = parse_config(raw)
        svc = config.services[0]
        self.assertEqual(svc.interval_seconds, 120)
        self.assertEqual(svc.alerting.failure_threshold, 4)
        self.assertEqual(svc.alerting.severity, "LOW")
        self.assertEqual(svc.alerting.recipients, ("ops@test.example",))  # default recipients

    def test_missing_required_field(self):
        raw = self._raw()
        del raw["services"][0]["request"]
        self.assertConfigError(raw, "'request' is a required property")

    def test_duplicate_service_ids(self):
        raw = self._raw()
        raw["services"].append(dict(raw["services"][0]))
        self.assertConfigError(raw, "duplicate service id 'svc-a'")

    def test_host_not_allowlisted(self):
        raw = self._raw(request={"url": "http://evil.example.com/ok", "method": "GET"})
        self.assertConfigError(raw, "not in security.allowed_hosts")

    def test_data_changing_method_rejected(self):
        raw = self._raw(request={"url": "http://127.0.0.1:8080/ok", "method": "DELETE"})
        self.assertConfigError(raw, "services/0/request/method")

    def test_read_only_must_be_true(self):
        raw = self._raw(read_only=False)
        self.assertConfigError(raw, "services/0/read_only")

    def test_operation_type_must_be_inquiry(self):
        raw = self._raw(operation_type="TRANSACTION")
        self.assertConfigError(raw, "services/0/operation_type")

    def test_literal_password_rejected(self):
        raw = self._raw(request={"url": "http://127.0.0.1:8080/ok", "method": "GET",
                                 "auth": {"type": "basic", "username": "u", "password": "Secret123"}})
        self.assertConfigError(raw, "services/0/request/auth")

    def test_sensitive_header_literal_rejected(self):
        raw = self._raw(request={"url": "http://127.0.0.1:8080/ok", "method": "GET",
                                 "headers": {"Authorization": "Bearer abc.def"}})
        self.assertConfigError(raw, "header 'Authorization' must be a secret reference")

    def test_forbidden_keyword_in_url(self):
        raw = self._raw(request={"url": "http://127.0.0.1:8080/api/payment/status", "method": "GET"})
        self.assertConfigError(raw, "forbidden keyword 'payment'")

    def test_interval_below_minimum(self):
        raw = self._raw(interval={"value": 5, "unit": "seconds"})
        self.assertConfigError(raw, "below limits.min_interval_seconds")

    def test_retry_budget_must_fit_interval(self):
        raw = self._raw(retry={"max_total_seconds": 600})
        self.assertConfigError(raw, "retry.max_total_seconds must be lower")

    def test_invalid_regex(self):
        raw = self._raw(validation={"body_format": "json",
                                    "rules": {"type": "json_regex", "path": "$.a", "pattern": "([a-z"}})
        self.assertConfigError(raw, "invalid regex")

    def test_invalid_json_path(self):
        raw = self._raw(validation={"body_format": "json", "rules": {"type": "json_exists", "path": "status"}})
        self.assertConfigError(raw, "must start with '$'")

    def test_xml_rule_requires_xml_body(self):
        raw = self._raw(validation={"body_format": "json", "rules": {"type": "soap_no_fault"}})
        self.assertConfigError(raw, "XML rules require body_format 'xml'")

    def test_redirects_need_global_permission(self):
        raw = self._raw(request={"url": "http://127.0.0.1:8080/ok", "method": "GET", "follow_redirects": True})
        self.assertConfigError(raw, "requires security.allow_redirects=true")

    def test_email_api_must_be_allowlisted(self):
        raw = make_raw_config(self.dir, [make_service("svc-a", "http://127.0.0.1:8080/ok")],
                              email_url="https://mail.other.example/send")
        self.assertConfigError(raw, "notifications.email_api.url rejected")

    def test_reminder_shorter_than_suppression(self):
        raw = self._raw(alerting={"suppression_interval_seconds": 600, "reminder_interval_seconds": 60})
        self.assertConfigError(raw, "reminder_interval_seconds must be >=")

    def test_duplicate_json_keys_rejected(self):
        path = os.path.join(self.dir, "dup.json")
        with open(path, "w") as handle:
            handle.write('{"environment": "UAT", "environment": "PROD"}')
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("duplicate key", ctx.exception.errors[0])

    def test_invalid_json_syntax(self):
        path = os.path.join(self.dir, "bad.json")
        with open(path, "w") as handle:
            handle.write('{"environment": ')
        with self.assertRaises(ConfigError) as ctx:
            load_config(path)
        self.assertIn("invalid JSON", ctx.exception.errors[0])

    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            load_config(os.path.join(self.dir, "nope.json"))

    def test_unknown_property_rejected(self):
        raw = self._raw()
        raw["services"][0]["intervall"] = {"value": 1, "unit": "minutes"}
        self.assertConfigError(raw, "Additional properties are not allowed")

    def test_env_reference_not_allowed_in_host(self):
        raw = self._raw(request={"url": "http://${env:HOST}:8080/ok", "method": "GET"})
        self.assertConfigError(raw, "only allowed in the path/query")

    def test_variables_global_and_service_override(self):
        raw = self._raw(variables={"customer_id": "111"},
                        request={"url": "http://127.0.0.1:8080/c/${var:customer_id}/${var:region}", "method": "GET"})
        raw["variables"] = {"customer_id": "999", "region": "eu"}
        svc = parse_config(raw).services[0]
        self.assertEqual(svc.variables, {"customer_id": "111", "region": "eu"})

    def test_undefined_variable_rejected(self):
        raw = self._raw(request={"url": "http://127.0.0.1:8080/c/${var:customer_id}", "method": "GET"})
        self.assertConfigError(raw, "${var:customer_id} is used but not defined")

    def test_nested_variable_rejected(self):
        raw = self._raw(variables={"a": "${var:b}", "b": "1"},
                        request={"url": "http://127.0.0.1:8080/c/${var:a}", "method": "GET"})
        self.assertConfigError(raw, "must not reference another")

    def test_numeric_variable_allowed(self):
        raw = self._raw(variables={"customer_id": 1000649149},
                        request={"url": "http://127.0.0.1:8080/c/${var:customer_id}", "method": "GET"})
        self.assertEqual(parse_config(raw).services[0].variables["customer_id"], "1000649149")

    def test_load_from_file_records_hash(self):
        path = write_config(self.dir, self._raw())
        config = load_config(path)
        self.assertEqual(len(config.content_hash), 64)
        self.assertEqual(config.path, os.path.abspath(path))


class AllowlistTests(unittest.TestCase):
    def test_entries(self):
        allow = HostAllowlist(["api.bank.internal", "gw.bank.internal:8443", "*.esb.bank.internal", "10.0.0.5:*"],
                              ["https"], ["transfer"])
        self.assertIsNone(allow.check("https://api.bank.internal/x"))
        self.assertIsNotNone(allow.check("https://api.bank.internal:8443/x"))  # non-default port
        self.assertIsNone(allow.check("https://gw.bank.internal:8443/x"))
        self.assertIsNone(allow.check("https://a.esb.bank.internal/x"))
        self.assertIsNotNone(allow.check("https://esb.bank.internal/x"))
        self.assertIsNotNone(allow.check("https://a.esb.bank.internal.evil.com/x"))
        self.assertIsNone(allow.check("https://10.0.0.5:9999/x"))
        self.assertIsNotNone(allow.check("http://api.bank.internal/x"))  # scheme
        self.assertIsNotNone(allow.check("https://user:pw@api.bank.internal/x"))
        self.assertIsNotNone(allow.check("https://api.bank.internal/fund-transfer"))

    def test_sanitize_endpoint(self):
        self.assertEqual(sanitize_endpoint("https://h.internal:8443/acct/1234567890/balance?pin=1"),
                         "https://h.internal:8443/acct/{id}/balance")


if __name__ == "__main__":
    unittest.main()
