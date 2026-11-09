"""Scrapling-HTTP-Client mit Rate-Limiting, Verbindungsabbruch-Erkennung und Browser-Fallback."""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Protocol

from scrapling.fetchers import FetcherSession

from flashscore.database import FlashscoreDatabase
from flashscore.models import utc_now_iso

logger = logging.getLogger("flashscore.client")


class HtmlTransport(Protocol):
    """Abstrakte Schnittstelle für HTML-Abrufe (Scrapling Live-Session oder Replay-Provider)."""

    def fetch_html(self, url: str, *, require_js: bool = False) -> str: ...

    def close(self) -> None: ...


class RateLimiter:
    """Thread-sicherer Rate-Limiter zur schonenden Abfrage der Quellseite."""

    def __init__(self, min_delay_seconds: float = 0.85, jitter_seconds: float = 0.25) -> None:
        self.min_delay_seconds = max(0.0, min_delay_seconds)
        self.jitter_seconds = max(0.0, jitter_seconds)
        self._lock = threading.Lock()
        self._last_request_ts = 0.0

    def wait(self) -> None:
        if self.min_delay_seconds <= 0:
            return
        with self._lock:
            now = time.monotonic()
            target = self._last_request_ts + self.min_delay_seconds + random.uniform(0.0, self.jitter_seconds)
            sleep_for = target - now
            if sleep_for > 0:
                time.sleep(sleep_for)
            self._last_request_ts = time.monotonic()


@dataclass
class ConnectionHealth:
    is_online: bool = True
    consecutive_failures: int = 0
    total_requests: int = 0
    total_failures: int = 0
    last_success_at: str | None = None
    last_failure_at: str | None = None
    outage_started_at: float | None = None
    last_error_message: str | None = None


class ConnectionMonitor:
    """Erkennt Verbindungsabbrüche sofort, berechnet Backoff-Zeiten und protokolliert Wiederherstellungen."""

    def __init__(self, db: FlashscoreDatabase | None = None) -> None:
        self._db = db
        self._lock = threading.Lock()
        self.health = ConnectionHealth()

    def record_success(self, url: str) -> None:
        with self._lock:
            was_offline = not self.health.is_online
            outage_secs = (
                round(time.monotonic() - self.health.outage_started_at, 2)
                if self.health.outage_started_at is not None
                else 0.0
            )
            self.health.is_online = True
            self.health.consecutive_failures = 0
            self.health.total_requests += 1
            self.health.last_success_at = utc_now_iso()
            self.health.outage_started_at = None
            self.health.last_error_message = None

        if was_offline:
            msg = f"Verbindung zu Flashscore wiederhergestellt nach {outage_secs}s ({url})"
            logger.info(msg)
            if self._db is not None:
                self._db.log_system_event(
                    "INFO",
                    "ConnectionMonitor",
                    "CONNECTION_RESTORED",
                    msg,
                    {"url": url, "outage_seconds": outage_secs},
                )

    def record_failure(self, url: str, error: Exception | str) -> float:
        err_str = str(error)
        with self._lock:
            first_drop = self.health.is_online
            if first_drop:
                self.health.outage_started_at = time.monotonic()
            self.health.is_online = False
            self.health.consecutive_failures += 1
            self.health.total_requests += 1
            self.health.total_failures += 1
            self.health.last_failure_at = utc_now_iso()
            self.health.last_error_message = err_str
            failures = self.health.consecutive_failures

        backoff = min(60.0, float(2 ** min(failures, 6)))
        lvl = "ERROR" if first_drop or failures % 5 == 0 else "WARNING"
        code = "CONNECTION_LOST" if first_drop else "FETCH_RETRY_BACKOFF"
        msg = (
            f"Verbindungsabbruch / Abruffehler bei {url} "
            f"(Fehler #{failures}, nächster Versuch in {backoff:.0f}s): {err_str}"
        )
        logger.warning(msg)
        if self._db is not None:
            self._db.log_system_event(
                lvl,
                "ConnectionMonitor",
                code,
                msg,
                {"url": url, "consecutive_failures": failures, "backoff_seconds": backoff},
            )
        return backoff


class ScraplingFlashscoreClient:
    """Produktiver HTTP-/Stealth-Client auf Basis von Scrapling (`FetcherSession` + optional `StealthySession`)."""

    def __init__(
        self,
        db: FlashscoreDatabase | None = None,
        *,
        min_delay_seconds: float = 0.85,
        timeout_seconds: int = 25,
        max_retries: int = 3,
        enable_browser_fallback: bool = False,
    ) -> None:
        self.rate_limiter = RateLimiter(min_delay_seconds=min_delay_seconds)
        self.monitor = ConnectionMonitor(db=db)
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(1, max_retries)
        self.enable_browser_fallback = enable_browser_fallback
        self._headers = {
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.7",
            "Referer": "https://www.flashscore.de/",
        }

    def fetch_html(self, url: str, *, require_js: bool = False) -> str:
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self.rate_limiter.wait()
            try:
                if require_js and self.enable_browser_fallback:
                    html = self._fetch_with_stealth_browser(url)
                else:
                    html = self._fetch_with_http_session(url)
                self.monitor.record_success(url)
                return html
            except Exception as exc:
                last_exc = exc
                backoff = self.monitor.record_failure(url, exc)
                if attempt < self.max_retries:
                    time.sleep(min(backoff, 5.0))

        raise ConnectionError(f"Abruf nach {self.max_retries} Versuchen fehlgeschlagen ({url}): {last_exc}")

    def _fetch_with_http_session(self, url: str) -> str:
        with FetcherSession(
            impersonate="chrome",
            stealthy_headers=True,
            timeout=self.timeout_seconds,
            retries=1,
        ) as session:
            response = session.get(url, headers=self._headers)
            status_code = int(getattr(response, "status", 200))
            if status_code >= 400:
                raise RuntimeError(f"HTTP-Status {status_code} für {url}")
            body = getattr(response, "html_content", None) or getattr(response, "body", b"")
            if isinstance(body, bytes):
                text = body.decode("utf-8", errors="replace")
            else:
                text = str(body)
            if not text.strip():
                raise RuntimeError(f"Leere Antwort von {url}")
            return text

    def _fetch_with_stealth_browser(self, url: str) -> str:
        from scrapling.fetchers import StealthySession

        with StealthySession(headless=True, solve_cloudflare=False) as session:
            page = session.fetch(url, network_idle=True)
            body = getattr(page, "html_content", None) or getattr(page, "body", b"")
            return body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)

    def close(self) -> None:
        return None
