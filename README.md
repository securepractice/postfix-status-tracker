# Postfix Status Tracker

Small cron-friendly parser for Postfix `mail.log` status events.

No virtualenv is required. The script uses Python 3 standard library only and is intended to run directly with system `python3` from cron.

## What It Does

- Reads new lines from `/var/log/mail.log` based on saved seek offset.
- Detects rotation/truncation when saved offset is larger than current `mail.log` size.
- On rotation, reads remaining lines from `/var/log/mail.log.1`, then continues from start of new `mail.log`.
- Extracts `queue_id` and `status` events (`sent`, `bounced`, `deferred`, `expired`, etc.).
- Sends one JSON batch payload to each configured HTTPS endpoint.
- Logs each run to syslog (`mail` facility) as `postfix-status-tracker`.

## Files

- `bin/postfix_status_tracker.py` - main script (Python stdlib only)
- `config/config.example.json` - config format example

## Typical Host Install (No venv)

Run these on the Postfix server as root:

```bash
install -d -m 0755 /opt/postfix-status-tracker/bin
install -d -m 0700 /etc/postfix-status-tracker
install -d -m 0755 /var/lib/postfix-status-tracker
install -d -m 0755 /var/run

install -m 0755 bin/postfix_status_tracker.py /opt/postfix-status-tracker/bin/postfix_status_tracker.py
install -m 0600 -o root -g root config/config.example.json /etc/postfix-status-tracker/config.json
```

Then edit `/etc/postfix-status-tracker/config.json` with real endpoint URLs and key/secret values.

Notes:

- Endpoint URLs must be `https://...`.
- Endpoint `auth_type` supports:
  - `headers` (default): sends `key` and `secret` in configured header names.
  - `basic`: sends `Authorization: Basic base64(key:secret)`.
  - `bearer`: sends `Authorization: Bearer <key>`.
- Events are sent in chunks using `batch_max_entries` (default: `500`).
- By default, no POST is sent when there are zero new entries (`"send_empty_batches": false`).
- Optional stderr debug logging can be enabled via `"debug_stderr": true` in config
  or per run with `--debug-stderr`.
- A lock file (`/var/run/postfix-status-tracker.lock`) prevents overlapping cron runs.
- The lock file stores PID metadata; if PID data is stale, it is automatically replaced on next successful run.

## Endpoint Authentication

Each endpoint entry always uses these common fields:

- `url`: target HTTPS endpoint
- `key`: credential value, username, or bearer token, depending on auth mode
- `secret`: credential value or password, depending on auth mode
- `timeout_sec`: HTTP timeout in seconds
- `verify_tls`: whether to verify the endpoint TLS certificate (`true` by default)

### Header Authentication

If `auth_type` is omitted, the script defaults to `headers` mode.

In this mode:

- `key` is sent in the header named by `key_header`
- `secret` is sent in the header named by `secret_header`
- `key_header` and `secret_header` are required in practice unless the defaults match your API

Example:

```json
{
  "name": "primary-api",
  "url": "https://api1.example.com/postfix/status",
  "auth_type": "headers",
  "key": "YOUR_API_KEY",
  "secret": "YOUR_API_SECRET",
  "verify_tls": true,
  "timeout_sec": 10,
  "key_header": "X-API-Key",
  "secret_header": "X-API-Secret"
}
```

This produces request headers like:

```text
X-API-Key: YOUR_API_KEY
X-API-Secret: YOUR_API_SECRET
```

### Basic Authentication

In this mode:

- `key` is used as the Basic Auth username
- `secret` is used as the Basic Auth password
- `key_header` and `secret_header` are ignored

Example:

```json
{
  "name": "primary-api",
  "url": "https://api1.example.com/postfix/status",
  "auth_type": "basic",
  "key": "YOUR_USERNAME",
  "secret": "YOUR_PASSWORD",
  "verify_tls": true,
  "timeout_sec": 10
}
```

This produces a request header like:

```text
Authorization: Basic <base64(key:secret)>
```

### Bearer Authentication

In this mode:

- `key` is used as the bearer token
- `secret` is ignored
- `key_header` and `secret_header` are ignored

Example:

```json
{
  "name": "primary-api",
  "url": "https://api1.example.com/postfix/status",
  "auth_type": "bearer",
  "key": "YOUR_BEARER_TOKEN",
  "secret": "IGNORED_FOR_BEARER",
  "verify_tls": true,
  "timeout_sec": 10
}
```

This produces a request header like:

```text
Authorization: Bearer YOUR_BEARER_TOKEN
```

If an endpoint uses a self-signed or private PKI certificate, you can set
`"verify_tls": false` for that endpoint only.

Quick dry-run check (executes parser logic, useful before enabling cron):

```bash
python3 /opt/postfix-status-tracker/bin/postfix_status_tracker.py --config /etc/postfix-status-tracker/config.json
```

## Config Security Requirement

The config file used by the script **must** have permissions `0400` or `0600`.

Example:

```bash
sudo install -m 0600 -o root -g root config/config.example.json /etc/postfix-status-tracker/config.json
```

If mode is anything else, the script exits with error and does not run.

## Cron.d Example

Create `/etc/cron.d/postfix-status-tracker`:

```cron
* * * * * root /usr/bin/env python3 /opt/postfix-status-tracker/bin/postfix_status_tracker.py --config /etc/postfix-status-tracker/config.json
```

Adjust script path to your install location.

Or create it directly with correct owner/mode:

```bash
cat >/etc/cron.d/postfix-status-tracker <<'EOF'
* * * * * root /usr/bin/env python3 /opt/postfix-status-tracker/bin/postfix_status_tracker.py --config /etc/postfix-status-tracker/config.json
EOF
chown root:root /etc/cron.d/postfix-status-tracker
chmod 0644 /etc/cron.d/postfix-status-tracker
```

## Logging

The script logs each run through syslog using the `mail` facility and the ident `postfix-status-tracker`.

Typical log messages include:

- startup failures such as bad config permissions, missing log file, invalid endpoint config, or HTTP errors
- lock contention when another cron run is already active
- rotation detection when the saved offset is larger than the current `mail.log` size
- batch delivery summaries including batch number and entry count
- a final run summary with parsed entry count and the new saved offset

When run manually, fatal errors are also printed to stderr in addition to being sent to syslog.

For troubleshooting, enable verbose debug output to stderr:

```bash
python3 /opt/postfix-status-tracker/bin/postfix_status_tracker.py \
  --config /etc/postfix-status-tracker/config.json \
  --debug-stderr
```

The same behavior can be enabled persistently via `"debug_stderr": true` in config.

## Payload Format

Payload follows `docs/REST-API-payload.json` shape:

```json
{
  "type": "simulation",
  "source": "smtp.example.net",
  "entries": [
    {
      "queue_id": "4XyzAB1234",
      "status": "sent",
      "timestamp": "2026-07-10T14:32:11+02:00"
    }
  ]
}
```

## License

This project is licensed under the Apache License 2.0. See `LICENSE` for the full text.
