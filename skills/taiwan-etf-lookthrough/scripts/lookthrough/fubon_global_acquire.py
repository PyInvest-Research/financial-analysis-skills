"""Bounded official Fubon global acquisition shared with daily publication."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
import signal
import threading
from time import monotonic
from typing import Any
from urllib.request import ProxyHandler, Request, build_opener

from .issuers import fubon_global
from .refresh import NoRedirect
from .transport_evidence import TransportEvidence


SYMBOLS = ('00903', '00920')
MAX_BYTES = 10 * 1024**2


@contextmanager
def _network_deadline(seconds: float) -> Iterator[None]:
    """Interrupt source headers/body at deadline in this standalone POSIX CLI.

    An active process timer or a non-main thread fails before networking.
    The previous signal handler is restored on success and on any failure.
    """
    if (threading.current_thread() is not threading.main_thread()
            or not hasattr(signal, 'setitimer')):
        raise ValueError('source_absolute_deadline_unavailable')
    if seconds <= 0 or any(signal.getitimer(signal.ITIMER_REAL)):
        raise ValueError('source_deadline_timer_unavailable')
    previous = signal.getsignal(signal.SIGALRM)

    def expired(signum: int, frame: Any) -> None:
        """Raise an authored timeout instead of accepting a late source."""
        raise TimeoutError('source_pair_deadline_exceeded')

    signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class _DeadlineResponse:
    """Expose framing metadata while bounding each network read by deadline."""

    def __init__(self, response: Any, remaining: Callable[[], float]) -> None:
        """Initialize bounded response state without network I/O."""
        self.response = response
        self.remaining = remaining
        self.read_one = getattr(response, 'read1', None)
        raw = getattr(getattr(response, 'fp', None), 'raw', None)
        sock = getattr(raw, '_sock', None)
        self.set_timeout = getattr(sock, 'settimeout', None)
        if not callable(self.read_one) or not callable(self.set_timeout):
            raise ValueError('source_deadline_controls_unavailable')
        self.remaining()

    def __getattr__(self, name: str) -> Any:
        """Delegate metadata access to the original response."""
        return getattr(self.response, name)

    def __enter__(self) -> Any:
        """Enter the original response context."""
        self.response.__enter__()
        return self

    def __exit__(self, *args: Any) -> Any:
        """Close the original response context."""
        return self.response.__exit__(*args)

    def read(self, size: int) -> bytes:
        """Return bounded body bytes or propagate the read failure."""
        remaining = self.remaining()
        if self.response.isclosed():
            return b''
        self.set_timeout(min(25, remaining))
        raw = self.read_one(size)
        self.remaining()
        return raw


class _DeadlineOpener:
    """Wrap existing transport with deadline-aware responses."""

    def __init__(self, opener: Any, remaining: Callable[[], float]) -> None:
        """Initialize bounded response state without network I/O."""
        self.opener = opener
        self.remaining = remaining

    def open(self, request: Request, *, timeout: float) -> _DeadlineResponse:
        """Wrap a response or close it if deadline controls are missing."""
        response = self.opener.open(request, timeout=timeout)
        try:
            return _DeadlineResponse(response, self.remaining)
        except Exception:
            response.close()
            raise


class SourceSession:
    """Two exact GETs maximum, no retry/redirect/cookie, stop shared host."""

    def __init__(self, transport: TransportEvidence | None = None) -> None:
        """Initialize bounded response state without network I/O."""
        self.deadline = monotonic() + 60
        self.attempted = set()
        self.stopped = False
        self.transport = transport or TransportEvidence()

    def remaining(self) -> float:
        """Return the positive remaining seconds or stop the pair."""
        value = self.deadline - monotonic()
        if value <= 0:
            raise ValueError('pair_deadline_exceeded')
        return value

    def get(self, symbol: str) -> bytes:
        """Fetch one exact source with framing and deadline checks."""
        if symbol not in SYMBOLS:
            raise ValueError('outside_pair_scope')
        url = fubon_global.FUNDS[symbol][1]
        if self.stopped or symbol in self.attempted or len(
                self.attempted) >= 2:
            raise ValueError('shared_host_or_request_cap_stop')
        self.attempted.add(symbol)
        try:
            remaining = self.remaining()
            opener = _DeadlineOpener(
                build_opener(ProxyHandler({}), NoRedirect()), self.remaining)
            request = Request(
                url,
                headers={
                    'User-Agent': 'PyInvest-ETF-Lookthrough/1.6.0',
                    'Accept-Encoding': 'identity'})

            def check(response: Any) -> None:
                """Reject changed source URLs, status or encoding."""
                if response.geturl() != url or response.getcode() != 200:
                    raise ValueError('changed_source_url_or_status')
                mime = response.getheader(
                    'Content-Type', '').split(';')[0].strip().lower()
                if mime != 'text/html' or response.getheader(
                        'Content-Encoding', 'identity') != 'identity':
                    raise ValueError('unexpected_source_encoding_or_type')

            with _network_deadline(self.remaining()):
                raw = self.transport.read(
                    opener, request, timeout=min(25, remaining),
                    limit=MAX_BYTES, remaining=self.remaining,
                    check_response=check)
            self.remaining()  # a late body is not accepted
            return raw
        except Exception:
            self.stopped = True
            raise
