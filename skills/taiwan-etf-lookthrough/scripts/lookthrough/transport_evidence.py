"""Run-local denial state and bounded official HTTP evidence, without secrets.

No networking or retry policy lives here: callers retain their qualified URL,
TLS opener, timeout, byte and request budgets. Content-Length is checked against
the received body before any text decoding; urllib does not decompress it.
"""

import hashlib
from http.client import IncompleteRead
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlsplit


CAPTURE_BYTES = 2 * 1024**2
CAPTURE_TOTAL = 8 * 1024**2  # each of publisher and agent; <=16 MiB per workflow
MAX_EVENTS = 1200
DENIED = {401, 403, 429}


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def location(url: str) -> dict:
    parts = urlsplit(url)
    path = quote(unquote(parts.path).rstrip("/") or "/", safe="/:@!$&'()*+,;=-._~")
    if len(path) > 2048:
        raise ValueError("Official source path exceeds evidence limit")
    return {"host": parts.hostname, "path": path,
            "url_sha256": sha(url.encode())}


class SharedDenial(ValueError):
    """An already denied route/host is skipped without issuing a request."""


class Denials:
    """Keep only structured 401/403/429 evidence for this acquisition run."""

    def __init__(self):
        self.records = []
        self.urls = set()
        self.routes = set()
        self.counts = {}

    def check(self, url: str) -> None:
        item = location(url)
        if self.counts.get(item["host"], 0) >= 3:
            raise SharedDenial("shared_host_denial")
        if (item["url_sha256"] in self.urls
                or (item["host"], item["path"]) in self.routes):
            raise SharedDenial("shared_route_denial")

    def add(self, item: dict) -> None:
        if item["http_status"] == 200:
            if self.counts.get(item["host"], 0):
                self.records.append(dict(item))
            self.counts[item["host"]] = 0
            return
        if item["http_status"] not in DENIED or item["url_sha256"] in self.urls:
            return
        self.records.append(dict(item))
        self.urls.add(item["url_sha256"])
        self.routes.add((item["host"], item["path"]))
        self.counts[item["host"]] = self.counts.get(item["host"], 0) + 1

    def record(self, url: str, status: int) -> None:
        if status in DENIED or status == 200:
            self.add(dict(location(url), http_status=status))

    def restore(self, records: list, hosts: set[str]) -> None:
        """Accept only the current publisher receipt's typed, reviewed data."""
        if not isinstance(records, list) or len(records) > MAX_EVENTS:
            raise ValueError("Invalid publisher denial ledger")
        for item in records:
            if (not isinstance(item, dict) or set(item) != {
                    "host", "path", "url_sha256", "http_status"}
                    or not isinstance(item["host"], str) or item["host"] not in hosts
                    or type(item["http_status"]) is not int
                    or item["http_status"] not in DENIED | {200}
                    or not isinstance(item["path"], str)
                    or not item["path"].startswith("/") or len(item["path"]) > 2048
                    or any(ord(c) < 32 or c in "?#\\" for c in item["path"])
                    or not isinstance(item["url_sha256"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", item["url_sha256"])):
                raise ValueError("Invalid publisher denial record")
        for item in records:
            self.add(item)


class BodyError(ValueError):
    """Partial bytes are diagnostics only and never returned as source bytes."""

    def __init__(self, reason: str, raw: bytes, receipt: dict):
        super().__init__(reason)
        self.partial = raw[:CAPTURE_BYTES]
        self.receipt = dict(receipt, complete=False,
                            eligible_for_validation=False, error_type=reason,
                            bytes_read=len(raw), sha256=sha(raw))


def read_body(response, limit: int, remaining=None) -> tuple[bytes, dict]:
    """Read at the existing limit; distinguish verified framing from unknown."""
    header = getattr(response, "getheader", lambda name: None)
    length, transfer = header("Content-Length"), header("Transfer-Encoding")
    transfer = transfer.lower().strip() if isinstance(transfer, str) else None
    receipt = {"content_length": None, "transfer_encoding": (
        transfer if transfer in (None, "chunked", "identity") else "unsupported"),
        "complete": None, "eligible_for_validation": False}
    status = getattr(response, "getcode", lambda: None)()
    receipt["http_status"] = status if type(status) is int and 100 <= status < 600 else None
    content_type = header("Content-Type")
    mime = content_type.split(";", 1)[0].strip().lower() if isinstance(content_type, str) else ""
    receipt["content_type"] = mime if re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", mime) and len(mime) <= 100 else None
    if length is not None:
        if not isinstance(length, str) or not re.fullmatch(r"[0-9]{1,12}", length):
            raise BodyError("invalid_content_length", b"", receipt)
        receipt["content_length"] = int(length)
        if int(length) > limit:
            raise BodyError("source_too_large", b"", receipt)
    if transfer not in (None, "identity", "chunked") or (transfer == "chunked" and length is not None):
        raise BodyError("ambiguous_http_framing", b"", receipt)
    raw = bytearray()
    try:
        if remaining is None:
            raw.extend(response.read(limit + 1))
        else:
            while True:
                remaining()
                part = response.read(min(65536, limit + 1 - len(raw)))
                raw.extend(part)
                if not part or len(raw) > limit:
                    break
    except IncompleteRead as error:
        raw.extend(error.partial[:max(0, limit + 1 - len(raw))])
        receipt["byte_count_scope"] = "retained_bytes_lower_bound"
        raise BodyError("IncompleteRead", bytes(raw), receipt) from error
    except OSError as error:
        raise BodyError(type(error).__name__, bytes(raw), receipt) from error
    if len(raw) > limit:
        raise BodyError("source_too_large", bytes(raw), receipt)
    if not raw:
        raise BodyError("empty_response", b"", receipt)
    if length is not None and len(raw) != int(length):
        raise BodyError("content_length_mismatch", bytes(raw), receipt)
    receipt.update(complete=True if length is not None or transfer == "chunked" else None,
                   integrity="verified_framing" if length is not None or transfer == "chunked" else "unknown_length",
                   eligible_for_validation=True, bytes_read=len(raw), sha256=sha(raw))
    return bytes(raw), receipt


class TransportEvidence:
    """Persist only bounded public source bodies and schema-authored metadata."""

    def __init__(self, folder: Path | None = None):
        self.folder = folder
        self.denials = Denials()
        self.events = []
        self.saved = 0
        self.skipped_events = 0
        self.last = None
        if folder is not None:
            if folder.exists():
                raise ValueError("Transport evidence directory must be fresh")
            folder.mkdir(parents=True)

    def append(self, event: dict) -> None:
        if len(self.events) < MAX_EVENTS:
            self.events.append(event)
        else:
            self.skipped_events += 1
        if self.folder is not None:
            value = {"schema_version": 1, "events": self.events,
                     "capture_bytes": self.saved, "skipped_events": self.skipped_events}
            value["run"] = {key: os.environ.get(key) if re.fullmatch(pattern, os.environ.get(key, "")) else None
                            for key, pattern in (("GITHUB_RUN_ID", r"[0-9]{1,20}"),
                                                 ("GITHUB_RUN_ATTEMPT", r"[0-9]{1,5}"),
                                                 ("GITHUB_SHA", r"[a-f0-9]{40}"))}
            path = self.folder / "ledger.json"
            temp = self.folder / "ledger.tmp"
            temp.write_text(json.dumps(value, allow_nan=False) + "\n")
            temp.replace(path)

    def save(self, url: str, receipt: dict, raw: bytes) -> None:
        item = dict(location(url), **receipt, event="download")
        self.last = dict(item)
        if self.folder is not None:
            partial = item.get("eligible_for_validation") is not True
            data = raw[:CAPTURE_BYTES] if partial else raw
            if len(data) <= CAPTURE_BYTES and self.saved + len(data) <= CAPTURE_TOTAL:
                child = ("partial-" if partial else "source-") + sha(data) + ".raw"
                path = self.folder / child
                if not path.exists():
                    path.write_bytes(data)
                    self.saved += len(data)
                item.update(capture=child, capture_sha256=sha(data), capture_bytes=len(data),
                            capture_truncated=len(data) != item.get("bytes_read", len(data)))
            else:
                item["capture_status"] = "capture_skipped_limit"
        self.append(item)

    def read(self, opener, request, *, timeout: float, limit: int, remaining=None,
             check_response=None) -> bytes:
        url = request.full_url
        try:
            self.denials.check(url)
        except SharedDenial as error:
            self.append(dict(location(url), event="skip", reason=str(error)))
            raise
        self.last = None
        try:
            with opener.open(request, timeout=timeout) as response:
                if check_response is not None:
                    check_response(response)
                raw, receipt = read_body(response, limit, remaining)
        except HTTPError as error:
            self.denials.record(url, error.code)
            self.save(url, {"http_status": error.code, "error_type": "HTTPError",
                           "complete": False, "eligible_for_validation": False,
                           "bytes_read": 0, "body_captured": False}, b"")
            raise
        except BodyError as error:
            self.save(url, error.receipt, error.partial)
            raise
        except Exception as error:
            self.save(url, {"error_type": type(error).__name__, "complete": False,
                           "eligible_for_validation": False, "bytes_read": 0}, b"")
            raise
        self.save(url, receipt, raw)
        if receipt.get("http_status") == 200:
            self.denials.record(url, 200)
        return raw
