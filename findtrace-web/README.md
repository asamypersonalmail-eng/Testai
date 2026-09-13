# FindTrace — Middleware Request Log Viewer

A small internal web application that provides a safe browser UI over the
existing `findtrace` Bash utility, so middleware/integration engineers can
search very large middleware log files by Request ID and view the full
request lifecycle without shell access to the log server.

```
Browser  →  REST API (Node/Express)  →  findtrace (Bash)  →  HDB middleware logs
                                              ↓
Browser  ←  JSON response (log text) ←───────┘
```

## Why Node.js / Express

Either Flask or Express would work; Express was chosen because:

- The whole stack (backend + frontend) is one language, which keeps an
  internal ops tool this small easier to hand off and maintain.
- Node's `child_process.spawn` with an argument array is a direct,
  well-understood way to invoke `findtrace` without a shell, matching the
  security requirement exactly.
- Deployment is a single `npm install` and `node server.js` — no virtualenv,
  no WSGI server needed for an internal tool at this scale.
- Node's stream-based `stdout`/`stderr` handling makes it straightforward to
  cap output size and enforce a timeout without buffering the whole log tail
  in memory beyond the configured cap.

Nothing here rules out a Flask port later — the API contract (`POST
/api/findtrace`) and the security model (argument-array `subprocess.run`,
no shell) translate directly.

## What this application does NOT do

- It does **not** reimplement `findtrace`'s log-scanning algorithm. The
  existing script is deliberately optimized for tens-of-millions-of-line log
  files using `grep` (to locate separator and match lines) and a single
  `sed` pass (to extract only the needed block ranges) instead of a
  line-by-line scan. Reimplementing that in the web backend would either be
  slower or would duplicate carefully-tuned logic for no benefit. The backend
  only launches the script and streams back what it prints.
- It does **not** read `HDB_LOG.txt` / `HDB_MWV2_LOG.txt` directly, in the
  backend or the browser.
- It does **not** use `findtrace -o`. The "Download .log" button in the UI
  builds the downloadable file client-side from the JSON response already
  received — it does not trigger another `findtrace` invocation or touch the
  server's filesystem.
- It does **not** build shell command strings from user input anywhere.
  `findtrace` is always invoked as `spawn(FINDTRACE_PATH, [flag, requestId],
  { shell: false })` — a fixed argv array, never a concatenated string
  passed through `sh -c`.

## Prerequisites

- Node.js 16 or later and npm.
- The `findtrace` script installed and executable on the same host (or a
  host reachable the same way this process is), by default at
  `/usr/local/bin/findtrace`.
- The OS user running this web app must be able to **execute** `findtrace`
  and, transitively, whatever `findtrace` itself needs to **read** the log
  files at `/var/mqm/HDB_LOGS/HDB_LOG.txt` and
  `/var/mqm/HDB_LOGS/HDB_MWV2_LOG.txt`. Root is not required — grant the
  service account read access to those two files (and execute access to the
  script) specifically, rather than running the whole service as root or as
  the `mqm` user.

## Installation

```bash
cd findtrace-web
npm install
```

## Configuration

Copy the example environment file and adjust it for your host:

```bash
cp .env.example .env
```

| Variable            | Default                       | Meaning                                                             |
|---------------------|--------------------------------|----------------------------------------------------------------------|
| `FINDTRACE_PATH`    | `/usr/local/bin/findtrace`     | Absolute path to the `findtrace` executable.                        |
| `FINDTRACE_TIMEOUT` | `60`                           | Seconds before an in-progress search is killed and an error is returned. |
| `MAX_OUTPUT_SIZE`   | `10485760` (10 MiB)            | Maximum bytes of `findtrace` stdout the backend will buffer/return. |
| `PORT`              | `8080`                         | Port the web server listens on.                                     |

None of these are hard-coded in the application code — `server.js` reads
them from the environment (via `dotenv`) at startup, and logs the resolved
values so you can confirm configuration on boot.

## Running

```bash
npm start
# or
node server.js
```

Then open `http://<host>:<port>/` in a browser.

## Running as a service

### systemd (Linux)

Create `/etc/systemd/system/findtrace-web.service`:

```ini
[Unit]
Description=FindTrace Middleware Log Viewer
After=network.target

[Service]
Type=simple
User=findtrace-svc
WorkingDirectory=/opt/findtrace-web
EnvironmentFile=/opt/findtrace-web/.env
ExecStart=/usr/bin/node server.js
Restart=on-failure
RestartSec=5
# Defense in depth: this process should not need broader filesystem access
# than the log files findtrace itself reads and its own working directory.
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now findtrace-web
sudo systemctl status findtrace-web
```

### AIX (System Resource Controller)

On AIX, wrap the same `node server.js` command with `mkssys`, or use a
simple `nohup`-based init script under `/etc/rc.d/rc2.d/` if `systemd`-style
management isn't available. Ensure `EnvironmentFile`-equivalent variables
(`FINDTRACE_PATH`, `FINDTRACE_TIMEOUT`, `MAX_OUTPUT_SIZE`, `PORT`) are
exported in the service's environment before `node server.js` starts, e.g.:

```bash
export FINDTRACE_PATH=/usr/local/bin/findtrace
export FINDTRACE_TIMEOUT=60
export MAX_OUTPUT_SIZE=10485760
export PORT=8080
nohup node /opt/findtrace-web/server.js >> /var/log/findtrace-web.log 2>&1 &
```

Run this under a dedicated, unprivileged service user that has been granted
execute permission on `findtrace` and read permission on the two log files
(via group membership or ACLs), not as root and not as `mqm` directly unless
your site's conventions require it.

## Security considerations

- **No shell interpolation.** The Request ID and source selector are the
  only values that ever reach `findtrace`, and they are passed as separate
  `argv` entries via `child_process.spawn(..., { shell: false })` — never
  concatenated into a command string, and never passed through `sh -c`,
  `bash -c`, or `eval`.
- **Input validation.** Request IDs are trimmed, capped at 200 characters,
  and restricted to `A-Z a-z 0-9 - _ . :`. Newlines and null bytes are
  rejected outright. The log source is restricted to the literal values
  `"1"` or `"2"`, mapped server-side to the fixed flags `-1`/`-2` — the
  browser can never supply a filename, a path, or an arbitrary flag.
- **No `-o`.** The web app never asks `findtrace` to write files to the log
  server's disk. "Download" is a client-side operation on data already
  returned to the browser.
- **Timeouts and output caps.** Every invocation is killed after
  `FINDTRACE_TIMEOUT` seconds and its stdout is capped at `MAX_OUTPUT_SIZE`
  bytes, so a runaway or unexpectedly large search cannot exhaust server
  memory or hang the process pool indefinitely.
  Bumped-into caps are surfaced as `truncated: true` in the API response
  rather than the browser silently getting a partial fake result.
- **No leaked internals.** Stack traces and raw `stderr` from `findtrace`
  are logged server-side only; the browser only ever sees short,
  human-readable error strings.
- **Output escaping.** All log content is HTML-escaped before being placed
  in the DOM. Highlighting is implemented by wrapping matches of a fixed,
  hard-coded keyword list inside already-escaped text — it never introduces
  markup derived from log content itself. Raw log text itself is only ever
  set via `textContent`, never `innerHTML`, when "Raw View" is active.
  Log data is never persisted to `localStorage`/`sessionStorage` — only
  Request ID, source, and timestamp are kept in per-tab session history.
- **Security headers.** Responses set `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, and a restrictive
  `Content-Security-Policy` limited to same-origin scripts/styles.
- **Least privilege.** Run the service under a dedicated OS account with
  execute rights on `findtrace` and read rights on the two log files —
  nothing more.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `The findtrace utility is not available on the server.` | `FINDTRACE_PATH` wrong, or the script isn't installed at that path. | Verify the path in `.env` matches where `findtrace` is actually installed; check with `which findtrace` or `ls -l <path>`. |
| `Permission denied while running the log search.` | Service account lacks execute permission on `findtrace`. | `chmod` / ACL the script so the service account can execute it, and confirm the file has its shebang and is not on a `noexec` filesystem. |
| `Unable to search the selected log source...` | `findtrace` exited non-zero — often because the log file itself is missing or unreadable. | Check `/var/mqm/HDB_LOGS/HDB_LOG.txt` / `HDB_MWV2_LOG.txt` exist and are readable by the service account; check server logs for the captured stderr. |
| Search always times out | Log file extremely large and slow storage, or `FINDTRACE_TIMEOUT` too low for this host. | Increase `FINDTRACE_TIMEOUT`; verify `findtrace` performs acceptably when run manually with `time findtrace -1 <id>`. |
| Result is cut off / "truncated" | Output exceeded `MAX_OUTPUT_SIZE`. | Increase `MAX_OUTPUT_SIZE` if the host has memory headroom, or narrow searches (more specific Request IDs). |
| Blank page / static assets 404 | App started from the wrong working directory. | Start with `node server.js` from inside `findtrace-web/` (or set `WorkingDirectory` in the service unit) so `public/` resolves correctly. |

## Request flow

```
1. Engineer enters a Request ID and picks a log source in the browser.
2. Browser POSTs { source, requestId } to /api/findtrace.
3. Express backend validates input, then spawns:
       findtrace <-1|-2> <requestId>
   as an argv array — no shell involved.
4. findtrace greps the huge log file for separator lines and Request ID
   matches, computes block ranges, and extracts them with a single sed pass.
5. Backend streams stdout back (capped and time-limited) and returns JSON:
   { success, found, requestId, source, executionTimeMs, output }.
6. Browser renders the escaped, highlighted log text in the Log Viewer,
   without ever loading the full underlying log file.
```

## UI overview (text description)

```
┌───────────────────────────────────────────────────────────────────────┐
│  FindTrace                                                    (navy)  │
│  Middleware Request Log Viewer                                        │
├───────────────────────────────────────────────────────────────────────┤
│  LOG SOURCE                                                            │
│  [● HDB_LOG.txt]   [○ HDB_MWV2_LOG.txt]                                │
│                                                                         │
│  REQUEST ID                                                            │
│  [ Enter Request ID..................................... ]            │
│                                                                         │
│  [ Search Logs ]  [ Clear ]                                            │
│                                                                         │
│  Recent Searches: [ABC-123 HDB_LOG.txt] [XYZ-987 HDB_MWV2_LOG.txt]     │
├───────────────────────────────────────────────────────────────────────┤
│  Request ID: ABC-123456   Source: HDB_LOG.txt                          │
│  Execution Time: 1.24 sec   Status: Found (green)                      │
├───────────────────────────────────────────────────────────────────────┤
│  [Copy] [Download .log] [Wrap Lines: OFF] [Expand] [Raw View: OFF]     │
│  Find in Result: [..............]  3 matches  [↑] [↓]                  │
│  ┌───────────────────────────────────────────────────────────────┐   │
│  │ ============================================================  │   │
│  │ 2026-09-13 09:40:15                                            │   │
│  │ RequestID: ABC-123456                                          │   │
│  │ Application: MobileBanking                                     │   │
│  │ Flow: getCustomerAccounts                                      │   │
│  │ HTTPRequest ...                              (blue, monospace) │   │
│  │ ERROR Timeout calling Backend            (red / orange, bold)  │   │
│  │ ============================================================  │   │
│  └───────────────────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────────────────┘
```

The workspace is light gray/white for readability over long sessions; the
log panel itself uses a dark background with a monospace font so
engineers can visually scan long blocks the way they would in a terminal,
with error/warning/HTTP/middleware keywords picked out in color.
