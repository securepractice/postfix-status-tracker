#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Postfix Status Tracker: cron-friendly Postfix queue status batch reporter.
"""Postfix Status Tracker.

This script is part of the postfix-status-tracker project:
https://github.com/securepractice/postfix-status-tracker

Purpose:
- read new Postfix queue status lines from mail.log
- survive normal log rotation using a saved byte offset
- report status batches to one or more HTTPS endpoints

Typical deployment is a once-per-minute cron job using system python3.
Configuration is expected in /etc/postfix-status-tracker/config.json.
"""

import argparse
import base64
import datetime as dt
import errno
import json
import os
import re
import socket
import ssl
import sys
import tempfile
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
	import fcntl
	import syslog
except ImportError as exc:
	sys.stderr.write(
		"ERROR: Missing required Python standard library module(s): "
		f"{exc}.\n"
	)
	sys.stderr.write(
		"Run this with a normal Linux system python3 installation. "
		"No virtualenv is required.\n"
	)
	raise SystemExit(2)


MONTHS = {
	"Jan": 1,
	"Feb": 2,
	"Mar": 3,
	"Apr": 4,
	"May": 5,
	"Jun": 6,
	"Jul": 7,
	"Aug": 8,
	"Sep": 9,
	"Oct": 10,
	"Nov": 11,
	"Dec": 12,
}

DEBUG_STDERR = False

# Match only the Postfix lines that already contain a final queue status.
LINE_RE = re.compile(
	r"^(?P<month>[A-Z][a-z]{2})\s+"
	r"(?P<day>\d{1,2})\s+"
	r"(?P<time>\d{2}:\d{2}:\d{2})\s+"
	r"(?P<host>\S+)\s+"
	r"postfix/[^\[]+\[\d+\]:\s+"
	r"(?P<queue_id>[A-Za-z0-9]+):\s+"
	r".*\bstatus=(?P<status>[a-z]+)\b"
)


@dataclass
class Endpoint:
	name: str
	url: str
	key: str
	secret: str
	auth_type: str
	timeout_sec: int
	key_header: str = "X-API-Key"
	secret_header: str = "X-API-Secret"
	verify_tls: bool = True


def log_info(message: str) -> None:
	syslog.syslog(syslog.LOG_INFO, message)


def log_error(message: str) -> None:
	syslog.syslog(syslog.LOG_ERR, message)


def log_debug_stderr(message: str) -> None:
	if not DEBUG_STDERR:
		return
	ts = dt.datetime.now(dt.timezone.utc).isoformat()
	print(f"DEBUG[{ts}] {message}", file=sys.stderr, flush=True)


def ensure_secure_config(config_path: Path) -> None:
	# Config contains endpoint credentials, so reject world/group-readable modes.
	st = config_path.stat()
	mode = st.st_mode & 0o777
	if mode not in (0o400, 0o600):
		raise PermissionError(
			f"Config file {config_path} has invalid mode {oct(mode)}; must be 0o400 or 0o600"
		)


def load_config(config_path: Path) -> Dict[str, Any]:
	# Keep config schema intentionally small: validate only the fields that affect
	# safe delivery and apply predictable defaults for the rest.
	ensure_secure_config(config_path)
	with config_path.open("r", encoding="utf-8") as f:
		cfg = json.load(f)

	endpoints_raw = cfg.get("endpoints", [])
	if not endpoints_raw:
		raise ValueError("Config must include at least one endpoint")

	endpoints: List[Endpoint] = []
	for item in endpoints_raw:
		parsed_url = urllib.parse.urlparse(item["url"])
		if parsed_url.scheme.lower() != "https":
			raise ValueError(
				f"Endpoint {item.get('name', '<unknown>')} must use HTTPS URL: {item['url']}"
			)

		verify_tls_raw = item.get("verify_tls", True)
		if not isinstance(verify_tls_raw, bool):
			raise ValueError(
				f"Endpoint {item.get('name', '<unknown>')} verify_tls must be true or false"
			)

		auth_type = str(item.get("auth_type", "headers")).lower()
		if auth_type not in ("headers", "basic", "bearer"):
			raise ValueError(
				f"Endpoint {item.get('name', '<unknown>')} has invalid auth_type '{auth_type}'. "
				"Use 'headers', 'basic', or 'bearer'."
			)

		timeout_sec = int(item.get("timeout_sec", 10))
		if timeout_sec <= 0:
			raise ValueError(
				f"Endpoint {item.get('name', '<unknown>')} timeout_sec must be > 0"
			)

		endpoints.append(
			Endpoint(
				name=item["name"],
				url=item["url"],
				key=item["key"],
				secret=item["secret"],
				auth_type=auth_type,
				timeout_sec=timeout_sec,
				key_header=item.get("key_header", "X-API-Key"),
				secret_header=item.get("secret_header", "X-API-Secret"),
				verify_tls=verify_tls_raw,
			)
		)

	cfg["_endpoints"] = endpoints
	cfg.setdefault("type", "postfix-status")
	cfg.setdefault("source", socket.gethostname())
	cfg.setdefault("log_file", "/var/log/mail.log")
	cfg.setdefault("state_file", "/var/lib/postfix-status-tracker/state.json")
	cfg.setdefault("lock_file", "/var/run/postfix-status-tracker.lock")
	cfg.setdefault("send_empty_batches", False)
	cfg.setdefault("batch_max_entries", 500)
	cfg.setdefault("debug_stderr", False)

	batch_max_entries = int(cfg.get("batch_max_entries", 500))
	if batch_max_entries <= 0:
		raise ValueError("batch_max_entries must be > 0")
	cfg["batch_max_entries"] = batch_max_entries

	debug_stderr_raw = cfg.get("debug_stderr", False)
	if not isinstance(debug_stderr_raw, bool):
		raise ValueError("debug_stderr must be true or false")
	cfg["debug_stderr"] = debug_stderr_raw
	return cfg


def pid_exists(pid: int) -> bool:
	if pid <= 0:
		return False
	try:
		os.kill(pid, 0)
		return True
	except OSError as exc:
		if exc.errno == errno.ESRCH:
			return False
		if exc.errno == errno.EPERM:
			return True
		return False


def read_lock_pid(fd: int) -> Optional[int]:
	try:
		os.lseek(fd, 0, os.SEEK_SET)
		raw = os.read(fd, 128).decode("utf-8", errors="replace").strip()
		if not raw:
			return None
		first = raw.splitlines()[0].strip()
		return int(first)
	except Exception:
		return None


def write_lock_metadata(fd: int) -> None:
	meta = {
		"pid": os.getpid(),
		"hostname": socket.gethostname(),
		"started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
	}
	data = f"{meta['pid']}\n{json.dumps(meta, separators=(',', ':'))}\n".encode("utf-8")
	os.lseek(fd, 0, os.SEEK_SET)
	os.ftruncate(fd, 0)
	os.write(fd, data)
	os.fsync(fd)


def clear_lock_metadata(fd: int) -> None:
	# Keep the lock file present, but clear PID metadata on graceful exit.
	os.lseek(fd, 0, os.SEEK_SET)
	os.ftruncate(fd, 0)
	os.fsync(fd)


def acquire_lock(lock_path: Path) -> int:
	# Cron may trigger a new run before the previous one exits. Keep a real
	# kernel lock and store PID metadata in the lock file for operator visibility.
	lock_path.parent.mkdir(parents=True, exist_ok=True)
	fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
	try:
		fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
		write_lock_metadata(fd)
		return fd
	except BlockingIOError:
		owner_pid = read_lock_pid(fd)
		os.close(fd)
		if owner_pid is not None:
			raise RuntimeError(
				f"Another instance is already running (pid={owner_pid}, lock: {lock_path})"
			)
		raise RuntimeError(f"Another instance is already running (lock: {lock_path})")


def read_state(state_path: Path) -> Dict[str, Any]:
	if not state_path.exists():
		return {"offset": 0}
	with state_path.open("r", encoding="utf-8") as f:
		data = json.load(f)
	if not isinstance(data.get("offset", 0), int):
		raise ValueError("State offset must be an integer")
	return data


def write_state_atomic(state_path: Path, state: Dict[str, Any]) -> None:
	# The saved offset is the only persistent parser state, so write it atomically
	# to avoid losing progress on partial writes or abrupt termination.
	state_path.parent.mkdir(parents=True, exist_ok=True)
	fd, tmp_name = tempfile.mkstemp(prefix=".state.", dir=str(state_path.parent))
	try:
		with os.fdopen(fd, "w", encoding="utf-8") as f:
			json.dump(state, f)
			f.flush()
			os.fsync(f.fileno())
		os.replace(tmp_name, state_path)
	finally:
		if os.path.exists(tmp_name):
			os.unlink(tmp_name)


def parse_line(line: str, tzinfo: dt.tzinfo, now: dt.datetime) -> Optional[Dict[str, str]]:
	# This matches the Postfix status lines we care about and ignores everything
	# else in mail.log without treating non-matches as errors.
	m = LINE_RE.match(line)
	if not m:
		return None

	month_name = m.group("month")
	day = int(m.group("day"))
	hh, mm, ss = [int(x) for x in m.group("time").split(":")]
	month = MONTHS.get(month_name)
	if not month:
		return None

	year = now.year
	ts = dt.datetime(year, month, day, hh, mm, ss, tzinfo=tzinfo)
	# Handle new-year overlap in syslog files.
	if ts - now > dt.timedelta(days=180):
		ts = ts.replace(year=year - 1)

	return {
		"queue_id": m.group("queue_id"),
		"status": m.group("status"),
		"timestamp": ts.isoformat(),
	}


def parse_events(file_path: Path, start_offset: int, tzinfo: dt.tzinfo, now: dt.datetime) -> Tuple[List[Dict[str, str]], int]:
	events: List[Dict[str, str]] = []
	with file_path.open("r", encoding="utf-8", errors="replace") as f:
		# Resume from the previous byte offset so cron runs only process new data.
		f.seek(start_offset)
		for line in f:
			evt = parse_line(line.rstrip("\n"), tzinfo, now)
			if evt:
				events.append(evt)
		end_offset = f.tell()
	return events, end_offset


def chunk_entries(entries: List[Dict[str, str]], size: int) -> List[List[Dict[str, str]]]:
	# Some receivers cap payload size, so split bursts into deterministic chunks.
	if size <= 0:
		raise ValueError("batch_max_entries must be > 0")
	return [entries[i : i + size] for i in range(0, len(entries), size)]


def validate_runtime_endpoint_url(url: str) -> None:
	# Defense-in-depth: validate again at call time to ensure urllib is never used
	# with unexpected schemes like file://.
	parsed = urllib.parse.urlparse(url)
	if parsed.scheme.lower() != "https":
		raise ValueError(f"Endpoint URL must use https scheme: {url}")
	if not parsed.netloc:
		raise ValueError(f"Endpoint URL must include network location: {url}")
	if parsed.username or parsed.password:
		raise ValueError(f"Endpoint URL must not embed credentials: {url}")


def post_payload(endpoint: Endpoint, payload_bytes: bytes) -> None:
	# Support fixed-header auth, HTTP Basic Auth, and Bearer tokens without
	# external dependencies.
	validate_runtime_endpoint_url(endpoint.url)
	headers = {
		"Content-Type": "application/json",
	}

	if endpoint.auth_type == "basic":
		token = base64.b64encode(f"{endpoint.key}:{endpoint.secret}".encode("utf-8")).decode("ascii")
		headers["Authorization"] = f"Basic {token}"
	elif endpoint.auth_type == "bearer":
		headers["Authorization"] = f"Bearer {endpoint.key}"
	else:
		headers[endpoint.key_header] = endpoint.key
		headers[endpoint.secret_header] = endpoint.secret

	# endpoint.url is validated before building the request and again at runtime.
	req = urllib.request.Request(
		endpoint.url,
		data=payload_bytes,
		method="POST",
		headers=headers,
	)
	ssl_context: Optional[ssl.SSLContext] = None
	if not endpoint.verify_tls:
		# Compatibility switch for endpoints using self-signed or private PKI certs.
		ssl_context = ssl.create_default_context()
		ssl_context.check_hostname = False
		ssl_context.verify_mode = ssl.CERT_NONE
	log_debug_stderr(
		f"POST endpoint={endpoint.name} url={endpoint.url} auth_type={endpoint.auth_type} "
		f"verify_tls={endpoint.verify_tls} timeout_sec={endpoint.timeout_sec} "
		f"payload_bytes={len(payload_bytes)}"
	)
	try:
		with urllib.request.urlopen(req, timeout=endpoint.timeout_sec, context=ssl_context) as resp:  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected  # nosec B310
			code = resp.getcode()
			log_debug_stderr(f"Endpoint {endpoint.name} responded with HTTP {code}")
			if code < 200 or code >= 300:
				raise RuntimeError(f"Endpoint {endpoint.name} responded with HTTP {code}")
	except urllib.error.HTTPError as exc:
		# Include a short response body snippet when available; this usually makes
		# auth and endpoint misconfiguration issues obvious from syslog alone.
		body_excerpt = ""
		try:
			body = exc.read().decode("utf-8", errors="replace").strip()
			if body:
				# Keep logs compact and single-line for easier syslog filtering.
				body_excerpt = " ".join(body.split())[:200]
		except Exception:
			body_excerpt = ""

		content_type = ""
		if exc.headers is not None:
			content_type = str(exc.headers.get("Content-Type", "")).lower()
		is_html = "html" in content_type
		if body_excerpt.lower().startswith(("<!doctype html", "<html")):
			is_html = True

		if body_excerpt:
			if is_html:
				raise RuntimeError(
					f"Endpoint {endpoint.name} HTTP {exc.code} (HTML error body omitted)"
				) from exc
			raise RuntimeError(
				f"Endpoint {endpoint.name} HTTP {exc.code}: {body_excerpt}"
			) from exc
		raise RuntimeError(f"Endpoint {endpoint.name} HTTP {exc.code}") from exc


def ensure_runtime_compatibility() -> None:
	# Keep the supported runtime explicit because this is meant for unattended
	# cron execution on servers, where Python versions can be old and varied.
	if sys.version_info < (3, 8):
		raise RuntimeError(
			"Python 3.8+ is required. Use system python3 (no virtualenv needed), "
			"for example: /usr/bin/python3 /opt/postfix-status-tracker/bin/postfix_status_tracker.py"
		)


def main() -> int:
	parser = argparse.ArgumentParser(
		description="Track Postfix queue statuses and POST batched updates.",
		epilog=(
			"Admin note: this tool uses only Python standard library modules. "
			"No virtualenv is required; run it with system python3."
		),
	)
	parser.add_argument(
		"--config",
		default="/etc/postfix-status-tracker/config.json",
		help="Path to JSON config file (must be mode 0400 or 0600)",
	)
	parser.add_argument(
		"--debug-stderr",
		action="store_true",
		help="Enable verbose debug output to stderr for manual troubleshooting",
	)
	args = parser.parse_args()

	# Use the mail facility so messages land near the surrounding Postfix logs.
	syslog.openlog(ident="postfix-status-tracker", facility=syslog.LOG_MAIL)

	lock_fd: Optional[int] = None
	try:
		global DEBUG_STDERR
		ensure_runtime_compatibility()
		# Load configuration before touching log/state files so config errors fail fast.
		config_path = Path(args.config)
		cfg = load_config(config_path)
		DEBUG_STDERR = bool(cfg.get("debug_stderr", False)) or bool(args.debug_stderr)
		log_debug_stderr(
			f"Loaded config={config_path} endpoints={len(cfg['_endpoints'])} "
			f"debug_stderr={DEBUG_STDERR}"
		)
		log_path = Path(cfg["log_file"])
		rotated_path = Path(f"{cfg['log_file']}.1")
		state_path = Path(cfg["state_file"])
		lock_path = Path(cfg["lock_file"])
		endpoints: List[Endpoint] = cfg["_endpoints"]
		send_empty_batches = bool(cfg.get("send_empty_batches", False))
		batch_max_entries = int(cfg.get("batch_max_entries", 500))

		lock_fd = acquire_lock(lock_path)
		log_debug_stderr(f"Acquired lock at {lock_path}")

		now = dt.datetime.now().astimezone()
		tzinfo = now.tzinfo
		if tzinfo is None:
			tzinfo = dt.timezone.utc

		state = read_state(state_path)
		prev_offset = int(state.get("offset", 0))
		if prev_offset < 0:
			prev_offset = 0

		if not log_path.exists():
			raise FileNotFoundError(f"Log file not found: {log_path}")

		current_size = log_path.stat().st_size
		log_debug_stderr(
			f"Read state offset={prev_offset} current_log_size={current_size} "
			f"log={log_path} rotated={rotated_path}"
		)
		all_events: List[Dict[str, str]] = []

		# Detect rotate/truncate by offset being larger than current file size.
		if prev_offset > current_size:
			# After rotation, the previous offset belongs to mail.log.1. Read the
			# remaining tail there first, then restart from byte 0 in the new file.
			log_info(
				f"Detected rotation/truncate: offset={prev_offset} current_size={current_size}"
			)
			if rotated_path.exists():
				rotated_events, _ = parse_events(rotated_path, prev_offset, tzinfo, now)
				all_events.extend(rotated_events)
				log_debug_stderr(
					f"Parsed rotated tail entries={len(rotated_events)} from {rotated_path}"
				)
				log_info(
					f"Parsed {len(rotated_events)} events from rotated file {rotated_path}"
				)
			prev_offset = 0

		current_events, new_offset = parse_events(log_path, prev_offset, tzinfo, now)
		all_events.extend(current_events)
		log_debug_stderr(
			f"Parsed current entries={len(current_events)} total_entries={len(all_events)} "
			f"new_offset={new_offset}"
		)

		if all_events or send_empty_batches:
			# Every endpoint receives the same logical batches so downstream systems
			# see the same entry grouping for a given cron run.
			batches = chunk_entries(all_events, batch_max_entries) if all_events else [[]]
			delivery_failures: List[str] = []

			for endpoint in endpoints:
				for idx, batch in enumerate(batches, start=1):
					log_debug_stderr(
						f"Delivering endpoint={endpoint.name} batch={idx}/{len(batches)} "
						f"entries={len(batch)}"
					)
					payload = {
						"type": cfg["type"],
						"source": cfg["source"],
						"entries": batch,
					}
					payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
					try:
						post_payload(endpoint, payload_bytes)
						log_info(
							f"Delivered batch {idx}/{len(batches)} with {len(batch)} entries "
							f"to endpoint={endpoint.name} url={endpoint.url}"
						)
					except Exception as exc:
						failure = (
							f"Endpoint delivery failed endpoint={endpoint.name} "
							f"batch={idx}/{len(batches)} entries={len(batch)}: {exc}"
						)
						delivery_failures.append(failure)
						log_error(failure)
						log_debug_stderr(f"Continuing after delivery failure: {failure}")
		else:
			log_info("No new entries detected; skipping endpoint POSTs")

		write_state_atomic(state_path, {"offset": new_offset})
		log_debug_stderr(f"Persisted state offset={new_offset} at {state_path}")
		if all_events or send_empty_batches:
			if delivery_failures:
				log_error(
					f"Run completed with endpoint delivery errors: "
					f"failed_batches={len(delivery_failures)}"
				)
				return 1
		log_info(
			f"Run complete: parsed_entries={len(all_events)} new_offset={new_offset} log={log_path}"
		)
		return 0
	except Exception as exc:
		message = f"Run failed: {exc}"
		log_error(message)
		if DEBUG_STDERR:
			traceback.print_exc(file=sys.stderr)
		# Mirror fatal errors to stderr so manual runs do not require syslog access.
		print(f"ERROR: {message}", file=sys.stderr)
		return 1
	finally:
		if lock_fd is not None:
			try:
				clear_lock_metadata(lock_fd)
			except Exception:
				# Best effort only; closing the fd still releases the kernel lock.
				pass
			os.close(lock_fd)


if __name__ == "__main__":
	sys.exit(main())
