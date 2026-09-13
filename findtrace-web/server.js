'use strict';

/**
 * FindTrace web backend.
 *
 * A thin, safety-focused wrapper around the existing `findtrace` Bash
 * utility. This process never reads the middleware log files itself and
 * never builds a shell command line from user input — it only spawns
 * `findtrace` with a fixed argument array.
 */

const path = require('path');
const crypto = require('crypto');
const { spawn } = require('child_process');
const express = require('express');

require('dotenv').config();

// ---------------------------------------------------------------------------
// Configuration (all overridable via environment / .env)
// ---------------------------------------------------------------------------

const CONFIG = {
  findtracePath: process.env.FINDTRACE_PATH || '/usr/local/bin/findtrace',
  timeoutMs: Number(process.env.FINDTRACE_TIMEOUT || 60) * 1000,
  maxOutputSize: Number(process.env.MAX_OUTPUT_SIZE || 10 * 1024 * 1024), // 10 MB
  port: Number(process.env.PORT || 8080),
};

// User-friendly display names for each log source. The web app never lets
// the browser choose an arbitrary file or flag — only "1" or "2", which are
// mapped here to the fixed findtrace flag and a label.
const LOG_SOURCES = {
  1: { flag: '-1', label: 'HDB_LOG.txt' },
  2: { flag: '-2', label: 'HDB_MWV2_LOG.txt' },
};

// Request IDs seen in these logs are alphanumeric plus a small set of
// separator characters. Anything else is rejected outright, well before it
// ever reaches a child process argument.
const REQUEST_ID_PATTERN = /^[A-Za-z0-9_.:-]+$/;
const MAX_REQUEST_ID_LENGTH = 200;

// ---------------------------------------------------------------------------
// App setup
// ---------------------------------------------------------------------------

const app = express();
app.use(express.json({ limit: '10kb' }));

// Basic security headers. No external framework needed for a handful of
// headers on an internal tool.
app.use((req, res, next) => {
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('X-Frame-Options', 'DENY');
  res.setHeader('Referrer-Policy', 'no-referrer');
  res.setHeader(
    'Content-Security-Policy',
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'"
  );
  next();
});

app.use(express.static(path.join(__dirname, 'public')));

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

function validateRequestId(rawId) {
  if (typeof rawId !== 'string') {
    return { error: 'Request ID is required.' };
  }
  const id = rawId.trim();
  if (id.length === 0) {
    return { error: 'Request ID is required.' };
  }
  if (id.length > MAX_REQUEST_ID_LENGTH) {
    return { error: `Request ID must be at most ${MAX_REQUEST_ID_LENGTH} characters.` };
  }
  if (id.includes('\n') || id.includes('\r')) {
    return { error: 'Request ID must not contain newline characters.' };
  }
  if (id.includes('\0')) {
    return { error: 'Request ID must not contain null bytes.' };
  }
  if (!REQUEST_ID_PATTERN.test(id)) {
    return { error: 'Request ID contains unsupported characters. Allowed: letters, numbers, - _ . :' };
  }
  return { value: id };
}

function validateSource(rawSource) {
  const key = String(rawSource);
  const source = LOG_SOURCES[key];
  if (!source) {
    return { error: 'Invalid log source selected.' };
  }
  return { value: source };
}

// ---------------------------------------------------------------------------
// findtrace invocation
// ---------------------------------------------------------------------------

/**
 * Runs findtrace with a strict argument array (never a shell string), a
 * timeout, and an output-size cap. Resolves with { stdout, stderr, code,
 * timedOut, truncated } — it never rejects on a non-zero exit, so callers
 * can inspect the result and decide how to respond.
 */
function runFindtrace(flag, requestId) {
  return new Promise((resolve) => {
    const child = spawn(CONFIG.findtracePath, [flag, requestId], {
      stdio: ['ignore', 'pipe', 'pipe'],
      // Explicitly not using a shell: argv is passed straight to exec(),
      // so shell metacharacters in requestId have no special meaning.
      shell: false,
    });

    let stdout = Buffer.alloc(0);
    let stderr = '';
    let truncated = false;
    let timedOut = false;
    let settled = false;

    const timer = setTimeout(() => {
      timedOut = true;
      child.kill('SIGKILL');
    }, CONFIG.timeoutMs);

    child.stdout.on('data', (chunk) => {
      if (truncated) return;
      if (stdout.length + chunk.length > CONFIG.maxOutputSize) {
        const remaining = CONFIG.maxOutputSize - stdout.length;
        if (remaining > 0) {
          stdout = Buffer.concat([stdout, chunk.subarray(0, remaining)]);
        }
        truncated = true;
        child.kill('SIGKILL');
        return;
      }
      stdout = Buffer.concat([stdout, chunk]);
    });

    child.stderr.on('data', (chunk) => {
      // Cap stderr too; it is only used for server-side logging.
      if (stderr.length < 8192) {
        stderr += chunk.toString('utf8');
      }
    });

    child.on('error', (err) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({ spawnError: err });
    });

    child.on('close', (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({
        stdout: stdout.toString('utf8'),
        stderr,
        code,
        timedOut,
        truncated,
      });
    });
  });
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

app.post('/api/findtrace', async (req, res) => {
  const requestLogId = crypto.randomUUID();
  const body = req.body || {};

  const sourceResult = validateSource(body.source);
  if (sourceResult.error) {
    return res.status(400).json({ success: false, error: sourceResult.error });
  }

  const idResult = validateRequestId(body.requestId);
  if (idResult.error) {
    return res.status(400).json({ success: false, error: idResult.error });
  }

  const source = sourceResult.value;
  const requestId = idResult.value;
  const startedAt = Date.now();

  let result;
  try {
    result = await runFindtrace(source.flag, requestId);
  } catch (err) {
    console.error(`[findtrace][${requestLogId}] unexpected error:`, err);
    return res.status(500).json({ success: false, error: 'Log search failed.' });
  }

  const executionTimeMs = Date.now() - startedAt;

  if (result.spawnError) {
    if (result.spawnError.code === 'ENOENT') {
      console.error(`[findtrace][${requestLogId}] executable not found at ${CONFIG.findtracePath}`);
      return res.status(500).json({
        success: false,
        error: 'The findtrace utility is not available on the server. Please contact an administrator.',
      });
    }
    if (result.spawnError.code === 'EACCES') {
      console.error(`[findtrace][${requestLogId}] permission denied executing ${CONFIG.findtracePath}`);
      return res.status(500).json({
        success: false,
        error: 'Permission denied while running the log search. Please contact an administrator.',
      });
    }
    console.error(`[findtrace][${requestLogId}] spawn error:`, result.spawnError);
    return res.status(500).json({ success: false, error: 'Log search failed.' });
  }

  if (result.timedOut) {
    console.error(`[findtrace][${requestLogId}] timed out after ${CONFIG.timeoutMs}ms`);
    return res.status(504).json({
      success: false,
      error: 'The log search took too long and was stopped. Try narrowing your search or contact an administrator.',
    });
  }

  if (result.code !== 0) {
    console.error(
      `[findtrace][${requestLogId}] exited with code ${result.code}; stderr: ${result.stderr.slice(0, 2000)}`
    );
    return res.status(500).json({
      success: false,
      error: 'Unable to search the selected log source. Please verify that the log file is available.',
    });
  }

  const output = result.stdout;
  const noResultPrefix = 'No entries found for Request ID:';

  if (output.trim().startsWith(noResultPrefix)) {
    return res.json({
      success: true,
      found: false,
      requestId,
      source: source.label,
      executionTimeMs,
      output: '',
    });
  }

  console.log(
    `[findtrace][${requestLogId}] source=${source.label} requestId=${requestId} ` +
      `bytes=${output.length} truncated=${result.truncated} timeMs=${executionTimeMs}`
  );

  return res.json({
    success: true,
    found: true,
    requestId,
    source: source.label,
    executionTimeMs,
    truncated: result.truncated,
    output,
  });
});

// Generic error handler as a last line of defense: never leak stack traces.
// eslint-disable-next-line no-unused-vars
app.use((err, req, res, next) => {
  console.error('Unhandled error:', err);
  res.status(500).json({ success: false, error: 'An unexpected server error occurred.' });
});

app.listen(CONFIG.port, () => {
  console.log(`FindTrace web server listening on port ${CONFIG.port}`);
  console.log(`findtrace executable: ${CONFIG.findtracePath}`);
  console.log(`timeout: ${CONFIG.timeoutMs}ms, max output size: ${CONFIG.maxOutputSize} bytes`);
});
