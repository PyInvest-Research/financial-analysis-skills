"""Fixed public Allianz collector; daily registration awaits live acceptance.

Four sequential reads, no retries, proxies, redirects or personal credentials.
Only freshly issued anonymous antiforgery cookies exist in this call's memory.
"""
from datetime import datetime, timezone
import hashlib
from http.client import HTTPSConnection
from http.cookies import SimpleCookie
import json
import re
import ssl
import sys
from time import monotonic
from urllib.parse import unquote

from .issuers import allianz
from .issuers._assets import read_json

HOST = "etf.allianzgi.com.tw"
PREFIX = "/webapi/api/"
PATHS = ("AntiForgery/GetAntiForgeryToken", "Fund/GetFundOverview",
         "Fund/GetFundAssets", "Fund/GetFundDetail")
BLOCKED_DER = "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b"
MIN_PATCH = {(3, 11): 18, (3, 12): 16, (3, 13): 18, (3, 14): 10, (3, 15): 2}
MAX_BYTES = 10 * 1024**2


class AcquisitionError(ValueError):
    """Only authored error codes, never response bodies or cookie values."""


def checked_context():
    """Check actual default trust without modifying its configuration."""
    version = sys.version_info
    if version[:2] not in MIN_PATCH or version[2] < MIN_PATCH[version[:2]]:
        raise AcquisitionError("allianz_runtime_patch_required")
    context = ssl.create_default_context()
    roots = context.get_ca_certs(binary_form=True)
    if (context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname
            or not roots or any(hashlib.sha256(c).hexdigest() == BLOCKED_DER for c in roots)):
        raise AcquisitionError("allianz_default_trust_not_qualified")
    return context


def _cookies(headers):
    """Narrow even a server's parent-domain cookie to this exact HTTPS host."""
    result = {}
    for header in headers:
        parsed = SimpleCookie()
        try:
            parsed.load(header)
        except Exception:
            raise AcquisitionError("allianz_cookie_schema") from None
        for name, cookie in parsed.items():
            if name != "X-XSRF-TOKEN" and not re.fullmatch(r"\.AspNetCore\.Antiforgery\.[A-Za-z0-9_-]+", name):
                continue
            if (name in result or cookie["domain"].lower().lstrip(".") not in ("", HOST, "allianzgi.com.tw")
                    or cookie["path"] not in ("", "/", "/webapi", "/webapi/")
                    or not re.fullmatch(r"[A-Za-z0-9_%+=./~-]{1,4096}", cookie.value)):
                raise AcquisitionError("allianz_cookie_scope_or_value")
            result[name] = cookie.value
    if "X-XSRF-TOKEN" not in result or len(result) != 2:
        raise AcquisitionError("allianz_missing_public_antiforgery")
    token = unquote(result["X-XSRF-TOKEN"], errors="strict")
    if not re.fullmatch(r"[A-Za-z0-9_+=./~-]{1,4096}", token):
        raise AcquisitionError("allianz_invalid_public_token")
    return result, token


def acquire(etf, *, deadline_utc=None, events=None):
    """Return exact public capture; credentials never enter it or the ledger.

    Each request is <=25 seconds; the sequence is <=100 seconds and an optional
    absolute UTC research deadline can only shorten it. DNS resolution remains
    subject to the OS resolver; no late response or follow-up is accepted.
    """
    if (etf.symbol not in {"00984A", "00993A"}
            or (etf.issuer, etf.is_active, etf.route, etf.source_url) != (
                "allianz", True, "http", allianz.SOURCE_URL)):
        raise AcquisitionError("allianz_unqualified_request")
    end = monotonic() + 100
    if deadline_utc is not None:
        deadline = datetime.fromisoformat(deadline_utc)
        if deadline.utcoffset() != timezone.utc.utcoffset(deadline):
            raise AcquisitionError("allianz_deadline_must_be_UTC")
        end = min(end, monotonic() + (deadline - datetime.now(timezone.utc)).total_seconds())
    def remaining(bound=end):
        value = bound - monotonic()
        if value <= 0:
            raise AcquisitionError("allianz_deadline_expired")
        return value
    remaining()
    context = checked_context()  # fail closed before creating any connection
    cookies, token = {}, None
    ledger = events if events is not None else []

    def request(index, body=None):
        bound = min(end, monotonic() + 25)
        remaining(bound)
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        encoded = None
        if body is not None:
            encoded = json.dumps(body, allow_nan=False).encode()
            headers.update({"Content-Type": "application/json", "X-XSRF-TOKEN": token,
                            "Cookie": "; ".join(k + "=" + v for k, v in cookies.items())})
        event = {"path": PREFIX + PATHS[index], "method": "GET" if body is None else "POST",
                 "started_at": datetime.now(timezone.utc).isoformat(), "status": "pending"}
        ledger.append(event)
        connection = None
        try:
            connection = HTTPSConnection(HOST, timeout=remaining(bound), context=context)
            connection.request(event["method"], event["path"], body=encoded, headers=headers)
            remaining(bound)
            # getresponse() may detach connection.sock on Connection: close.
            # Retain the underlying socket while the response owns its read file.
            sock = connection.sock
            if sock is not None:
                sock.settimeout(remaining(bound))
            with connection.getresponse() as response:
                event["http_status"] = response.status
                if response.status != 200:
                    raise AcquisitionError("allianz_HTTP_" + str(response.status))
                if response.getheader("Content-Encoding", "identity").lower() != "identity":
                    raise AcquisitionError("allianz_encoded_body_unsupported")
                if response.getheader("Content-Type", "").split(";", 1)[0].lower() != "application/json":
                    raise AcquisitionError("allianz_non_json_or_challenge")
                length = response.getheader("Content-Length")
                cap = 65536 if index == 0 else MAX_BYTES
                if (not isinstance(length, str) or not re.fullmatch(r"[0-9]{1,12}", length)
                        or not 0 < int(length) <= cap or response.getheader("Transfer-Encoding") is not None):
                    raise AcquisitionError("allianz_unknown_or_oversized_framing")
                chunks, size = [], 0
                while True:
                    remaining(bound)
                    if sock is not None:
                        sock.settimeout(remaining(bound))
                    part = response.read1(min(65536, cap + 1 - size))
                    remaining(bound)
                    if not part:
                        break
                    chunks.append(part)
                    size += len(part)
                    if size > cap:
                        raise AcquisitionError("allianz_body_too_large")
                raw = b"".join(chunks)
                if size != int(length):
                    raise AcquisitionError("allianz_short_body")
                event.update(bytes=size, length_verified=True)
                if index == 0:
                    value = read_json(raw)
                    if not isinstance(value, dict) or value.get("RedirectUrl"):
                        raise AcquisitionError("allianz_public_initialization_schema")
                    public_cookies, public_token = _cookies(response.headers.get_all("Set-Cookie", []))
                    event.update(status="ephemeral_initialization", body_persisted=False)
                    return public_cookies, public_token
                event.update(status="public_raw_received", sha256=hashlib.sha256(raw).hexdigest())
                return raw
        except Exception as error:
            reason = str(error) if isinstance(error, AcquisitionError) else "allianz_" + type(error).__name__
            event.update(status="stopped", error=reason)
            raise AcquisitionError(reason) from None
        finally:
            headers.clear()
            if connection is not None:
                connection.close()
            event["ended_at"] = datetime.now(timezone.utc).isoformat()

    try:
        cookies, token = request(0)
        overview = request(1, {"Keyword": etf.symbol, "FundNo": "", "FundType": -1,
                               "PageSize": 999, "PageIndex": 1})
        rows = allianz._response(overview, 1)
        if not isinstance(rows, list) or len(rows) != 1:
            raise AcquisitionError("allianz_identity_not_unique")
        allianz._identity(rows[0], etf.symbol)  # identity before assets request
        assets = request(2, {"FundID": rows[0]["CFundNo"]})
        allianz._response(assets, 0)  # stop on application refusal before detail
        detail = request(3, {"FundNo": rows[0]["CFundNo"], "IsPreview": False})
        allianz._response(detail, 0)
        return allianz.capture(overview, assets, detail)
    finally:
        cookies.clear()
        token = None
