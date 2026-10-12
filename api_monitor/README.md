# API Synthetic Monitor

A lightweight, standalone tool that sends controlled requests to read-only
inquiry APIs (ACE REST flows, DataPower SOAP gateways and similar), checks the
responses against configurable rules, records the results and sends email
alerts through your existing middleware email API.

- Python 3.8+ only. It needs no database, message broker or web framework.
  Its three runtime libraries are pure Python: `requests`, `jsonschema` and `defusedxml`.
- Everything is driven by JSON: endpoints, payloads, schedules, validation
  rules, alert policies and the email API contract. Changing any of them needs
  no code change.
- An HTTP 200 alone does not count as healthy. The business payload is always
  validated, so for example `{"status":"FAILED","errorCode":"CORE_TIMEOUT"}` is
  reported as a failure.
- It runs on Linux (systemd), AIX (SRC, inittab or cron) and Windows (Task Scheduler), either as a daemon
  or in single-cycle mode.

---

## Quick start: pull and run

### Get the code

```bash
git clone https://github.com/asamypersonalmail-eng/Testai.git
cd Testai
git checkout HealthCheck            # already cloned? run: git fetch origin && git checkout HealthCheck && git pull
cd api_monitor
```

### Windows (PowerShell)

Requires Python 3.8+ from python.org, with "Add python.exe to PATH" ticked.

```powershell
py -3 -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt

# 1. Prove it works (no network needed): 130 tests plus a demo
.\venv\Scripts\python.exe -m unittest discover -s tests -t .
.\venv\Scripts\python.exe examples\demo.py

# 2. Make your own config and point the paths at Windows folders
copy config.json my-config.json
#    edit my-config.json: logging.directory -> C:\\ProgramData\\api-monitor\\logs
#                         state.directory   -> C:\\ProgramData\\api-monitor\\state
#                         runtime.lock_file -> C:\\ProgramData\\api-monitor\\state\\monitor.lock
#                         security.ca_bundle -> your internal CA .pem (or null)
#    (in JSON every backslash is written twice, or use forward slashes: C:/ProgramData/api-monitor/logs)

# 3. Secrets for this PowerShell window only
$env:APIMON_POSITION_CUSTOMER_ID = "<synthetic test customer id>"
$env:APIMON_EMAIL_API_KEY = "..."

# 4. Validate, try one service, then run
.\venv\Scripts\python.exe monitor.py -c my-config.json validate --check-secrets
.\venv\Scripts\python.exe monitor.py -c my-config.json check get-position    # dry run, no email
.\venv\Scripts\python.exe monitor.py -c my-config.json --console run         # Ctrl+C to stop
```

To run it unattended on Windows, pick **one** option:

- **Task Scheduler (recommended):** copy the project to `C:\api-monitor`, put the config
  in `C:\ProgramData\api-monitor\config.json` and the secrets in
  `C:\ProgramData\api-monitor\secrets.env` (same `KEY=value` format as
  `deploy/systemd/secrets.env.example`). Restrict that file's NTFS permissions to
  Administrators and the account that runs the task. Then, in an elevated PowerShell,
  run `deploy\windows\install-task.ps1`. It runs `once` every 5 minutes, and each
  service is checked only when its own interval is due.
- **Windows service:** wrap `deploy\windows\run-monitor.ps1 -Mode run` with a service
  wrapper such as NSSM or WinSW (they are not bundled), set to restart on failure.

Windows differences:
- There is no `SIGHUP`. Config changes are picked up by the file watcher (`reload.watch_interval_seconds`).
- Stop with Ctrl+C, Ctrl+Break, or by stopping the service or task.
- Locking uses `msvcrt`, so a second copy is still refused (exit code 3).
- `${file:…}` secret files are not permission-checked on Windows; protect them with NTFS ACLs instead.
- The Windows code paths were written to be compatible but have only been tested on Linux.
  Run the test suite on the Windows host before relying on it.

### Linux / AIX

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python -m unittest discover -s tests -t .
cp config.json my-config.json             # edit paths, hosts, recipients
export APIMON_POSITION_CUSTOMER_ID=...    # and the other APIMON_* secrets
./venv/bin/python monitor.py -c my-config.json validate --check-secrets
./venv/bin/python monitor.py -c my-config.json check get-position
./venv/bin/python monitor.py -c my-config.json --console run
```

For a permanent installation, see section 2 (systemd, AIX SRC, cron).

---

## 1. Project layout

| File | Responsibility |
|---|---|
| `monitor.py` | Entry point and CLI (`run`, `once`, `check`, `validate`, `status`, `report`, `test-email`), signals, single-instance lock |
| `config.json` | Sample production configuration (3 services, email API mapping) |
| `config.schema.json` | JSON Schema (draft 2020-12) for the configuration |
| `config_loader.py` | Loads the config, runs schema and semantic validation, merges defaults, host allowlist |
| `scheduler.py` | Per-service scheduler: concurrency, rate limits, jitter, no overlap, reload, single-cycle mode |
| `checker.py` | One check: request, validation, bounded retries, state/alerts, structured log |
| `http_client.py` | Secure HTTP: allowlist, TLS verification, timeouts, size cap, redirect policy, error classification |
| `validators.py` | Response validation engine (status, content type, JSON/XML/SOAP rules, AND/OR/NOT, response time) |
| `alert_manager.py` | Incident state machine, thresholds, suppression, reminders, escalation, recovery |
| `email_client.py` | Adapter for the middleware email API (template mapping, circuit breaker, hourly cap) |
| `state_manager.py` | Persistent health state (atomic writes, `fcntl` locking, corruption quarantine) |
| `logging_config.py` | JSON logs, daily rotation, retention, secret/PII redaction |
| `secrets_resolver.py` | `${env:…}`, `${file:…}` and `${gen:…}` references |
| `rate_limiter.py` | Token bucket and sliding window limiters |
| `reporting.py` | Statistics, P50/P95/P99, failure trends, alert history, CSV export |
| `models.py` | Shared enums and data classes |
| `platform_compat.py` | Cross-platform file locking (`fcntl` on Linux/AIX, `msvcrt` on Windows) |
| `tests/` | 130 unit and integration tests (real sockets against local mock servers) |
| `examples/demo.py` | Self-contained demo of healthy, failed, degraded and recovered services |
| `deploy/systemd/` | systemd unit, timer alternative, secrets env example |
| `deploy/aix/` | ksh wrapper, SRC registration, crontab example |
| `deploy/windows/` | PowerShell runner (loads secrets) and Task Scheduler installer |

---

## 2. Installation

### Linux

```bash
sudo useradd --system --home /var/lib/api-monitor --shell /sbin/nologin apimon
sudo mkdir -p /opt/api-monitor /etc/api-monitor/certs /var/lib/api-monitor /var/log/api-monitor
sudo cp -r ./* /opt/api-monitor/
sudo python3 -m venv /opt/api-monitor/venv
sudo /opt/api-monitor/venv/bin/pip install -r /opt/api-monitor/requirements.txt
#   (offline / air-gapped: pip download -r requirements.txt -d wheels/ on a connected host,
#    then pip install --no-index --find-links wheels/ -r requirements.txt)

sudo cp /opt/api-monitor/config.json /etc/api-monitor/config.json          # then edit it
sudo cp /opt/api-monitor/deploy/systemd/secrets.env.example /etc/api-monitor/secrets.env
sudo cp internal-ca-chain.pem /etc/api-monitor/certs/
sudo chown -R root:apimon /etc/api-monitor && sudo chmod 750 /etc/api-monitor
sudo chmod 640 /etc/api-monitor/config.json && sudo chmod 600 /etc/api-monitor/secrets.env
sudo chown apimon:apimon /var/lib/api-monitor /var/log/api-monitor
sudo chmod 750 /var/lib/api-monitor /var/log/api-monitor

sudo cp /opt/api-monitor/deploy/systemd/api-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo -u apimon env $(sudo cat /etc/api-monitor/secrets.env | xargs) \
     /opt/api-monitor/venv/bin/python /opt/api-monitor/monitor.py -c /etc/api-monitor/config.json validate --check-secrets
sudo systemctl enable --now api-monitor
```

### AIX

1. Install Python 3.8+ (IBM AIX Toolbox `python3`) and create a venv in `/opt/api-monitor/venv`.
2. Install the requirements. The pins are deliberately pure Python: `jsonschema==4.17.3`
   avoids the Rust-compiled `rpds-py` used by newer releases. If the system
   OpenSSL is older than 1.1.1, replace `urllib3==2.2.3` with `urllib3==1.26.20`.
3. Copy the files as above. Use `chmod 600` for `secrets.env`.
4. Pick one run mode:
   - **SRC (recommended for daemon mode):** run `deploy/aix/install_src.sh`, then `startsrc -s apimon`.
     SRC restarts the subsystem on abnormal exit, and `stopsrc -s apimon` sends SIGTERM for a graceful stop.
     For boot start use `mkitab 'apimon:2:once:/usr/bin/startsrc -s apimon'`.
   - **inittab respawn:** `mkitab "apimon:2:respawn:su - apimon -c '/opt/api-monitor/deploy/aix/api-monitor.sh run'"`.
   - **cron / Control-M / TWS / AutoSys (single-cycle):** see `deploy/aix/crontab.example`.
     `once` checks only the services whose own interval has elapsed, based on the
     persisted last-check time. A single 5-minute cron entry therefore still honours
     the 60, 30 and 15 minute schedules in `config.json`.

> Use exactly one run mode per host and config: the daemon or a scheduler. If two
> modes start anyway, the lock file refuses the second instance (exit code 3).

---

## 3. CLI

```text
python monitor.py -c CONFIG run                     # continuous monitoring (daemon)
python monitor.py -c CONFIG once [--all] [--service ID ...]
                                                    # one cycle; only due services unless --all / --service
python monitor.py -c CONFIG check SERVICE_ID [--apply]
                                                    # on-demand check; dry run (no state, no email) unless --apply
python monitor.py -c CONFIG validate [--check-secrets]
python monitor.py -c CONFIG status [--json]
python monitor.py -c CONFIG report [--hours 24] [--service ID] [--csv out.csv]
python monitor.py -c CONFIG test-email [--recipient addr]
```

Add `--console` to also log to stderr. The config path can also come from `API_MONITOR_CONFIG`.

| Exit code | Meaning |
|---|---|
| 0 | OK |
| 1 | At least one check failed (`once` / `check`) |
| 2 | Configuration error (systemd does not restart on this) |
| 3 | Another instance holds the lock |
| 4 | Runtime error |

Signals: `SIGTERM`/`SIGINT` stop the monitor gracefully (in-flight checks get
`limits.shutdown_grace_seconds`). `SIGHUP` reloads the configuration.

---

## 4. Configuration reference

The file is validated by `config.schema.json` (structure, types, enums, secret
references) and then by semantic checks: allowlists, forbidden keywords,
timeout and retry budgets versus intervals, rule compilation, sensitive headers
and recipients. Unknown keys are rejected so typos cannot be silently ignored.
Duplicate JSON keys are rejected.

### 4.1 Global settings

| Key | Purpose (default) |
|---|---|
| `environment` | `PROD` / `UAT` / `SIT` / `DEV` / `DR`; shown in every log line and email |
| `monitor_host_id` | Host identifier in alerts (default: hostname) |
| `monitoring_enabled` | Master switch. When `false` the daemon idles and keeps watching the config |
| `defaults.interval` | `{"value": N, "unit": "seconds"\|"minutes"\|"hours"}` (15 min) |
| `defaults.timeout` | `connect_seconds`, `read_seconds`, `total_seconds` (hard deadline incl. body read) |
| `defaults.expected_status` | Default accepted HTTP status codes (`[200]`) |
| `defaults.retry` | See 4.3 |
| `defaults.response_time` | `warning_ms` (DEGRADED) and `critical_ms` (FAILURE) |
| `defaults.alerting` | See 4.5 |
| `limits.max_concurrent_checks` | Worker threads (4) |
| `limits.max_requests_per_cycle` | Max dispatches per scheduler tick / per `once` run (20) |
| `limits.max_requests_per_minute` | Global token bucket for all HTTP requests incl. retries (30) |
| `limits.max_retries_per_minute` | Global retry budget; prevents retry storms during outages (10) |
| `limits.max_response_bytes` | The body is read up to this size, then `RESPONSE_TOO_LARGE` (1 MiB) |
| `limits.min_interval_seconds` | Guard against over-polling production (60) |
| `limits.startup_jitter_seconds` / `schedule_jitter_seconds` | Random spread of first and subsequent runs |
| `limits.shutdown_grace_seconds` | Wait for in-flight checks on stop (30) |
| `security.allowed_hosts` | **Allowlist.** `host` (default port only), `host:8443`, `host:*`, `*.domain` |
| `security.allowed_schemes` | Default `["https"]` |
| `security.allowed_methods` | `GET`, `POST`, `HEAD` only (POST for SOAP / JSON inquiry bodies) |
| `security.allow_redirects` | Global master switch for redirects (false). Services must also opt in |
| `security.forbidden_url_keywords` | URLs/query keys containing these words are refused (e.g. `transfer`, `payment`) |
| `security.ca_bundle` | Internal CA chain (PEM). TLS verification cannot be disabled |
| `security.use_environment_proxies` / `proxies` | Env proxies are ignored unless enabled; explicit proxies allowed |
| `logging.*` | `directory`, `level`, `rotation: daily`, `retention_days`, `event_history_retention_days`, `console` |
| `state.directory` / `state.file` | Persistent state location |
| `runtime.lock_file` | Single-instance lock (put it next to the state) |
| `reload.enabled` / `watch_interval_seconds` / `on_sighup` | Config hot reload (file watch + SIGHUP) |
| `reporting.history_size` / `stats_log_interval_seconds` / `csv_summary_path` | In-memory stats (bounded), periodic `STATS_SUMMARY` log and CSV |
| `notifications.*` | See 4.6 |

### 4.2 Service settings

```jsonc
{
  "id": "customer-account-inquiry",       // unique, lowercase
  "name": "Customer Account Inquiry",
  "enabled": true,
  "operation_type": "INQUIRY",            // mandatory constant: only inquiries can be monitored
  "read_only": true,                      // mandatory constant
  "endpoint_label": "ESB-GW AccountInquiry v2",   // shown in logs/emails instead of the URL
  "request": {
    "url": "https://esb-gw.prod.bank.internal:8443/api/v2/accounts/inquiry",
    "method": "POST",
    "headers": {"X-Request-Id": "${gen:uuid}"},
    "query_params": {"customerId": "${env:APIMON_TEST_CUSTOMER_ID}"},
    "auth": {"type": "bearer", "token": "${env:APIMON_ESB_TOKEN}"},
    "body": {"type": "json", "content": {...}},     // or {"type": "raw", "content": "<soap…>", "content_type": "text/xml"}
    "follow_redirects": false, "max_redirects": 0,
    "ca_bundle": null, "client_cert": {"cert_file": "...", "key_file": "..."}   // optional mTLS
  },
  "interval": {"value": 60, "unit": "minutes"},
  "timeout": {...}, "retry": {...}, "validation": {...}, "response_time": {...}, "alerting": {...}
}
```

**Authentication types:** `none`, `basic` (`username`, `password`), `bearer`
(`token`), `api_key` (`header`, `value`), plus optional `client_cert` for mTLS.
Every credential must be a reference: `${env:NAME}` or `${file:/path}` (the file
must not be group- or world-readable). The schema rejects literal passwords and
tokens. Headers such as `Authorization` or `X-API-Key` must also be references.

A `${env:…}` reference may also appear in the URL **path or query** (for example a
synthetic customer number in `/v1/customers/${env:APIMON_POSITION_CUSTOMER_ID}/position`),
so the identifier stays out of `config.json` and out of the logs. References in the
host are rejected; the resolved URL is allowlist-checked again before every request.

**Generators** for request IDs and timestamps: `${gen:uuid}`, `${gen:uuid_short}`,
`${gen:timestamp_iso}`, `${gen:epoch_ms}`, `${gen:date}`.

### 4.3 Retry policy

| Key | Meaning |
|---|---|
| `max_attempts` | 1–5 attempts per check (including the first) |
| `backoff_initial_seconds`, `backoff_multiplier`, `backoff_max_seconds` | Bounded exponential backoff |
| `jitter_seconds` | Random jitter added to each backoff |
| `max_total_seconds` | Time budget for the whole check incl. retries; must be below the interval |
| `retry_on` | `CONNECTION_ERROR`, `TIMEOUT`, `DNS_ERROR`, `HTTP_5XX` (501 never), `INVALID_RESPONSE`, `RESPONSE_VALIDATION_FAILED` |
| `retry_when_down` | Default `false`: no retries against a service already known DOWN (one probe per interval) |

Retries also consume the global `max_retries_per_minute` and
`max_requests_per_minute` budgets. When either is exhausted, the check ends
without retrying.

### 4.4 Validation rules

```jsonc
"validation": {
  "expected_status": [200],
  "content_type": "application/json",   // compared without parameters
  "body_format": "json",                // json | xml | text | none
  "namespaces": {"soapenv": "http://schemas.xmlsoap.org/soap/envelope/"},   // for XML rules
  "rules": { "all": [ ... ] }           // all | any | not, nested up to 10 levels
}
```

| Rule `type` | Fields | Notes |
|---|---|---|
| `json_exists` / `json_not_exists` | `path` | JSONPath subset: `$.a.b`, `$.list[0]`, `$.list[*].x`, `$['k y']` |
| `json_equals` / `json_not_equals` | `path`, `value`, `quantifier` (`all`\|`any`) | Strict typing (`true` ≠ `1`, `"1"` ≠ `1`) |
| `json_in` | `path`, `values` | Allowed value list |
| `json_regex` | `path`, `pattern` | Regex runs on one approved field only, never the whole body (input capped at 4 KB) |
| `json_min_items` | `path`, `min` | Array/object size |
| `xml_exists` / `xml_not_exists` | `xpath`, `attribute`? | ElementTree XPath subset with `namespaces` |
| `xml_equals` / `xml_in` / `xml_regex` | `xpath`, `value`/`values`/`pattern`, `attribute`? | |
| `soap_no_fault` | – | Detects SOAP 1.1/1.2 `Fault` and reports faultcode/faultstring |
| `header_equals` / `header_regex` | `name`, `value`/`pattern` | Response headers |
| `body_contains` | `value`, `negate` | Plain substring |

Every rule accepts `description`, which is shown in alerts, and
`sensitive: true`, which reports the actual value as `<redacted>`. Observed
values are always masked (8+ digit numbers keep only their last 4 digits) and
truncated. XML is parsed with `defusedxml`: DTDs, entities and external
references are refused, which blocks XXE and entity-expansion attacks.

**Response time:** above `warning_ms` the outcome is DEGRADED with class
`RESPONSE_TIME_EXCEEDED`. Above `critical_ms` it is a FAILURE with the same class.

**Failure classes:** `CONNECTION_ERROR`, `DNS_ERROR`, `TLS_ERROR`, `HTTP_ERROR`,
`TIMEOUT`, `INVALID_RESPONSE`, `RESPONSE_TOO_LARGE`,
`RESPONSE_VALIDATION_FAILED`, `RESPONSE_TIME_EXCEEDED`, `CONFIGURATION_ERROR`
(missing secret, allowlist violation, unreadable CA). `INTERNAL_ERROR` is a
monitor bug: it is logged but never marks the service down.

### 4.5 Alert policy and state machine

| Key | Meaning |
|---|---|
| `enabled` | Per-service notifications. State is still tracked when disabled |
| `severity` | `CRITICAL` / `HIGH` / `MEDIUM` / `LOW` (mapped to an email priority) |
| `failure_threshold` | Consecutive failures before the incident opens and the ALERT is sent |
| `recovery_threshold` | Consecutive successes before the incident closes and RECOVERY is sent |
| `suppression_interval_seconds` | Minimum gap between alerts for the service (new incidents included) |
| `reminder_interval_seconds` | REMINDER while the incident stays open (`0` = off; must be ≥ suppression) |
| `alert_on_slow_response`, `slow_response_threshold` | Open a DEGRADED incident after N slow checks |
| `send_recovery` | Recovery notification on/off |
| `recipients`, `cc` | Override `notifications.default_recipients` / `default_cc` |

```
UNKNOWN/HEALTHY ─fail (<threshold)─▶ DEGRADED ─fail (≥threshold)─▶ DOWN  [ALERT]
HEALTHY ─slow ×N (if enabled)─▶ DEGRADED [ALERT] ─fail ≥threshold─▶ DOWN [ESCALATION]
DOWN ─success─▶ RECOVERING ─success ×recovery_threshold─▶ HEALTHY [RECOVERY]
RECOVERING ─fail─▶ DOWN (same incident, no new alert)
open incident ─every reminder_interval─▶ [REMINDER]
```

A failed email delivery marks the alert `pending`. The next scheduled check of
that service retries it once. There is no tight retry loop, and the circuit
breaker and hourly cap still apply. A RECOVERY email is only sent if an alert
for that incident was actually delivered.

### 4.6 Email middleware API mapping

The middleware contract is **not** hard-coded. Configure it under
`notifications.email_api`:

| Key | Purpose |
|---|---|
| `url`, `method` (`POST`/`PUT`) | Endpoint (must be in `security.allowed_hosts`) |
| `headers`, `auth`, `ca_bundle`, `client_cert` | Same semantics as services; secrets as references |
| `connect_timeout_seconds`, `read_timeout_seconds` | Explicit timeouts |
| `max_attempts` (≤3), `retry_backoff_seconds` | Per-email attempts. Retries only on connection errors, timeouts and 5xx; 4xx is never retried |
| `success_status_codes` | e.g. `[200, 201, 202]` |
| `success_body_format`, `success_rules` | Optional business-level acceptance check (same rule syntax as 4.4) |
| `payload_format` | `json`, `form` or `raw` (e.g. SOAP envelope / XML) |
| `payload_template` | Object (`json`/`form`) or string (`raw`) with `{{placeholders}}` |
| `raw_content_type`, `raw_escape` (`xml`/`json`/`none`) | For `raw` templates; values are escaped |
| `recipient_format` | `list`, `comma`, `semicolon`, `object_list` (+ `recipient_object_key`, `recipient_object_extra`) |
| `severity_priority_map` | Maps severity to the middleware priority value |
| `circuit_breaker.failure_threshold`, `open_seconds` | Stop calling the API after N consecutive failed sends |

Also under `notifications`: `enabled`, `recovery_notifications`,
`max_emails_per_hour` (global cap), `sender`, `default_recipients`,
`default_cc`, `subject_template`, `body_template` (optional; the default body
contains every required field), and `body_format` (`text`/`html`).

A JSON string that is exactly `"{{name}}"` is replaced by the typed value, so
`"to": "{{recipients}}"` can become a list or a list of objects. `${env:…}`
references in the template are resolved before alert values are inserted, so
text from a monitored response can never trigger secret resolution.

**Available placeholders:** `recipients`, `recipients_csv`, `cc`, `sender`,
`subject`, `body`, `body_format`, `content_type`, `priority`, `severity`,
`environment`, `service_id`, `service_name`, `service_label`, `endpoint`,
`event_type` (ALERT/REMINDER/ESCALATION/RECOVERY/TEST), `timestamp`,
`check_result`, `state`, `classification`, `http_status`, `response_time_ms`,
`expected`, `actual`, `consecutive_failures`, `consecutive_successes`,
`incident_id`, `incident_kind`, `incident_started_at`, `incident_detected_at`,
`incident_duration`, `alert_count`, `monitor_host`, `execution_id`, `alert_id`,
`attempts`.

**Fields to get from the real email API specification before go-live:**

1. Endpoint URL, HTTP method and the TLS CA chain or client certificate it requires.
2. The authentication scheme and the credential source (vault path / env var name).
3. Mandatory headers: channel ID, correlation/request ID, content type, API key.
4. Payload format (JSON / form / SOAP / XML) and the exact field names for sender,
   to, cc, subject, body, body content type and priority.
5. The recipient structure: an array of strings, a delimited string, or objects with a type (TO/CC).
6. Allowed priority or importance values (fill `severity_priority_map`).
7. Size limits for subject and body, and whether HTML is allowed.
8. What counts as success: HTTP codes and any business status field (fill `success_rules`).
9. Which error codes are retryable, and the API's own rate limits (align `max_emails_per_hour`).
10. Whether the sender address must be pre-registered or allow-listed.

Verify the mapping with `monitor.py test-email` before enabling alerting.

---

## 5. Production safety controls

| Control | Implementation |
|---|---|
| Only allowlisted endpoints | `security.allowed_hosts` checked at config load and before every request, including redirect targets and the email API |
| Read-only / inquiry only | `operation_type: INQUIRY` and `read_only: true` are mandatory; only `GET`, `POST` and `HEAD`; `forbidden_url_keywords` |
| No automatic redirects | Global `allow_redirects` **and** per-service `follow_redirects`; target re-checked; `Authorization` dropped on host change |
| TLS | Verification is always on; internal CA via `ca_bundle`; optional mTLS |
| Strict timeouts | Connect and read timeouts plus a total deadline enforced while streaming the body |
| Bounded load | Concurrency limit, per-tick cap, global request and retry token buckets, jitter, no retries against DOWN services, no catch-up bursts after suspend, restart-aware scheduling |
| No overlap | A service is never dispatched while its previous check is running; a single-instance `flock` covers the whole process |
| Secrets | Only `${env:…}` / `${file:…}` references; resolved values are registered and redacted from all logs |
| No sensitive data in logs/state | Payloads, response bodies and headers are never logged or persisted; observed values are masked/truncated; `sensitive: true` per rule; URLs with query strings never appear in logs |
| Email API failures | Bounded attempts, circuit breaker (persisted), hourly cap, serialized sends, pending alert retried once per check |
| Resource bounds | Fixed thread pool without a queue, bounded history deques, response size cap, rotated logs with retention, state pruned to configured services |
| Config safety | Full validation before start and before every reload; an invalid reload is rejected and the old config is kept |

Use dedicated synthetic test identities (test account, card reference,
customer) approved by the business and security owners. Never use real
customer data.

---

## 6. Logging, state and reporting

- `monitor.log` holds one JSON object per line: `ts` (UTC), `level`, `event`,
  `environment`, `service_id`, `execution_id`, `http_status`,
  `response_time_ms`, `validation_outcome`, `failure_reason`, `state`,
  `notification_status`, `attempts`, `incident_id`.
- `events.log` is the incident, alert and recovery history: `INCIDENT_OPENED`,
  `ALERT_SENT`, `ALERT_SUPPRESSED`, `REMINDER_SENT`, `INCIDENT_ESCALATED`,
  `INCIDENT_RESOLVED`, `RECOVERY_SENT`, `ALERT_DELIVERY_FAILED`,
  `EMAIL_CIRCUIT_OPENED/CLOSED` and `STATE_CHANGED`. It has a longer retention.
- Both logs rotate at UTC midnight. `retention_days` and
  `event_history_retention_days` control how many files are kept.
- `health_state.json` stores counters, timestamps, classifications and incident
  metadata only. It is written atomically (temp file, fsync, rename) under an
  `fcntl` lock. A corrupt file is quarantined (`*.corrupt-<epoch>`) and
  monitoring restarts from UNKNOWN.
- `report --hours 24 [--csv file]` rebuilds per-service checks, availability,
  P50/P95/P99/max latency, failure reasons, daily failure trend, alert and
  recovery counts, and the event history from the log files. The daemon also
  logs `STATS_SUMMARY` and can rewrite `reporting.csv_summary_path` periodically.

Reset one service's state by stopping the monitor, editing or removing its
entry in `health_state.json`, and starting again. Removing the whole file
resets everything.

---

## 7. Failure scenarios

| Scenario | Behaviour | Test |
|---|---|---|
| Connection refused | `CONNECTION_ERROR`, retried per policy | `test_http_client.test_connection_refused` |
| DNS failure | `DNS_ERROR` (not retried by default) | `test_http_client.test_dns_failure` |
| TLS handshake/certificate error | `TLS_ERROR` with the reason (e.g. `CERTIFICATE_VERIFY_FAILED`) | `test_http_client.test_tls_handshake_error` |
| HTTP 500/503 | `HTTP_ERROR` with status; retried when `HTTP_5XX` is in `retry_on` | `test_integration.test_timeout_and_invalid_responses_are_classified`, `test_retry_with_backoff_recovers_transient_error` |
| HTTP 200 with business error | `RESPONSE_VALIDATION_FAILED`, actual value in alert | `test_integration.test_outage_alert_suppression_and_recovery` |
| Backend timeout | `TIMEOUT` (connect/read/total deadline) | `test_http_client.test_read_timeout` |
| Invalid JSON / XML | `INVALID_RESPONSE` (DTD/XXE refused) | `test_validators` |
| Unexpected structure | `RESPONSE_VALIDATION_FAILED` (missing path) | `test_validators.test_unexpected_structure` |
| Slow backend | DEGRADED / `RESPONSE_TIME_EXCEEDED`, optional alert | `test_integration.test_slow_backend_degraded` |
| Email API unavailable | Monitoring continues, alert stays pending, circuit opens, traffic bounded | `test_integration.test_email_api_unavailable_does_not_stop_monitoring` |
| Invalid configuration | Exit 2 at start; reload rejected with the old config kept | `test_config`, `test_scheduler.test_reload_applies_valid_and_rejects_invalid_config` |
| Process restart | State, incidents and schedule resume; no duplicate alert | `test_alert_manager.test_state_survives_restart_without_duplicate_alert`, `test_integration.test_once_mode_persists_state_across_process_restarts`, `test_scheduler.test_restart_respects_previous_last_check` |
| Network instability | Bounded retries with backoff and jitter; retry budget | `test_integration.test_retry_with_backoff_recovers_transient_error` |
| Two checks at once | Same service never overlaps; a second process is refused | `test_scheduler.test_no_overlapping_execution_of_same_service`, `test_integration.test_second_instance_is_refused` |
| Many services failing | Independent incidents; global email cap; pending alerts | `test_integration.test_multiple_services_failing_with_hourly_cap` |
| Recovery after outage | RECOVERING, then HEALTHY and a RECOVERY email | `test_alert_manager.test_recovery_after_threshold` |

---

## 8. Testing

```bash
cd api_monitor
python3 -m unittest discover -s tests -t .      # 130 tests, about 20 s, no external network
python3 examples/demo.py                         # timeline of healthy / failed / degraded / recovered
```

The suite has been run against the pinned `requirements.txt` on Python 3.11
and 3.13. The tests start local HTTP servers on `127.0.0.1` for the backends and
the email API, so no external calls are made.

---

## 9. Production-readiness checklist

**Approvals and scope**
- [ ] Every monitored operation is confirmed read-only (inquiry) by the API owner; no side effects such as audit fees, OTPs or notifications.
- [ ] Synthetic test identities (account / card / customer) are approved, flagged as test data in core systems, and excluded from customer reporting.
- [ ] Polling frequency and concurrency are agreed with backend owners and capacity management.
- [ ] Change/CAB record covers the new outbound traffic from the monitoring host.

**Security**
- [ ] `allowed_hosts` lists only the required gateway hosts and ports; `allowed_schemes` is `https` only.
- [ ] `forbidden_url_keywords` reviewed for your API naming conventions.
- [ ] Internal CA chain installed; `ca_bundle` points to it; mTLS certificates have `600` permissions.
- [ ] Secrets are in the vault / `secrets.env` (`600`, root-owned), never in `config.json` or git.
- [ ] `validate --check-secrets` passes as the service user.
- [ ] Service account has least privilege on the ESB/DataPower side (inquiry operations only).
- [ ] Firewall rules allow only the monitoring host to reach the endpoints and the email API.

**Configuration**
- [ ] `environment`, `monitor_host_id`, recipients and severities set per environment.
- [ ] Each service validates business status fields, not only HTTP 200.
- [ ] `sensitive: true` set on rules whose values could identify a customer.
- [ ] Response-time thresholds are based on observed P95/P99 from UAT.
- [ ] `failure_threshold`, `suppression_interval_seconds` and `reminder_interval_seconds` are agreed with the support teams.

**Email integration**
- [ ] Field mapping completed from the middleware API specification (section 4.6).
- [ ] `test-email` delivers to a test mailbox; HTML/text rendering checked.
- [ ] `max_emails_per_hour` and the circuit breaker align with the email API's rate limits.
- [ ] Monitoring of the monitor: alert if `events.log` shows `EMAIL_CIRCUIT_OPENED`, or if the process or log is stale (e.g. log shipping or host monitoring).

**Operations**
- [ ] Exactly one run mode configured (systemd/SRC daemon **or** cron/timer).
- [ ] Log directory capacity is sized for the retention; logs are shipped to SIEM/Splunk/ELK if required.
- [ ] Runbook covers start/stop/reload, status, report, state reset and how to silence a service (`enabled: false` + reload).
- [ ] Failover and DR: the same config deployed on the DR host is disabled (`monitoring_enabled: false`) until activated.
- [ ] UAT soak test completed with injected failures: backend stop, 503, business error, slow response, email API down.
- [ ] Unit and integration tests pass on the target host's Python and OpenSSL.
