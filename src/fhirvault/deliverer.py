from __future__ import annotations

import hashlib
import hmac
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .store import Store

# A delivery is tried at most three times; after the first and second failure
# the worker waits one and two seconds respectively before trying again.
MAX_ATTEMPTS = 3
RETRY_DELAYS = (1.0, 2.0)
IDLE_WAIT = 0.5


def _parse_instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class Deliverer:
    """Posts subscription events to channel endpoints in a background thread.

    Pending deliveries live in the ``deliveries`` table, so work survives a
    process restart: on startup the worker resumes every row still in
    ``pending``, honouring the persisted ``not_before`` backoff. Each delivery
    keeps its ``delivery_id`` — and therefore its ``X-FhirVault-Delivery``
    header, sequence, and signature — on every retry, so receivers can
    deduplicate redeliveries.
    """

    def __init__(
        self,
        store: Store,
        delays: tuple[float, float] = RETRY_DELAYS,
        *,
        wall_clock: Callable[[], datetime] | None = None,
        transport: Callable[[str, bytes, dict[str, str]], tuple[int | None, str | None]] | None = None,
    ):
        self.store = store
        self.delays = delays
        # Scheduling uses a real wall clock independent of the store's clock
        # (which tests freeze for deterministic timestamps); persisted
        # not_before values are therefore honored across process restarts.
        self.wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self.transport = transport or self._post
        self._condition = threading.Condition()
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="fhirvault-deliverer", daemon=True)
        self._thread.start()

    def close(self) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        self._thread.join(timeout=5)

    def wake(self) -> None:
        """Interrupt the idle wait so a newly committed delivery starts at once."""
        with self._condition:
            self._condition.notify_all()

    # ------------------------------------------------------------------ worker

    def _run(self) -> None:
        while True:
            wait_for = self._process_due()
            with self._condition:
                if self._stopping:
                    return
                self._condition.wait(timeout=wait_for)

    def _next_due(self) -> tuple[dict[str, Any] | None, float]:
        """Return the earliest pending delivery if it is due (plus the time to
        wait otherwise). The clock is only consulted while work is pending, so
        an idle worker leaves the service clock untouched."""
        with self.store.lock:
            row = self.store.connection.execute(
                # Fresh work (NULL not_before) is due at its creation time;
                # retries are due at their persisted backoff instant.
                "SELECT subscription_id, sequence, delivery_id, endpoint, secret, payload, attempts, not_before "
                "FROM deliveries WHERE state = 'pending' "
                "ORDER BY CASE WHEN not_before IS NULL THEN created_at ELSE not_before END LIMIT 1"
            ).fetchone()
            if row is None:
                return None, IDLE_WAIT
            if row["not_before"] is not None:
                remaining = (_parse_instant(row["not_before"]) - self.wall_clock()).total_seconds()
                if remaining > 0:
                    return None, max(0.01, min(5.0, remaining))
            return {
                "subscription_id": row["subscription_id"],
                "sequence": row["sequence"],
                "delivery_id": row["delivery_id"],
                "endpoint": row["endpoint"],
                "secret": row["secret"],
                "body": row["payload"].encode("utf-8"),
                "attempts": row["attempts"],
            }, 0.0

    def _process_due(self) -> float:
        """Attempt every currently due delivery; return seconds until the next."""
        while True:
            delivery, wait_for = self._next_due()
            if delivery is None:
                return wait_for
            http_status, error = self.transport(
                delivery["endpoint"],
                delivery["body"],
                self._headers(delivery),
            )
            success = http_status is not None and 200 <= http_status < 300
            if not success and error is None and http_status is not None:
                error = f"endpoint responded with HTTP {http_status}"
            self._record_attempt(delivery, success, http_status, error)

    def _headers(self, delivery: dict[str, Any]) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-FhirVault-Subscription": delivery["subscription_id"],
            "X-FhirVault-Sequence": str(delivery["sequence"]),
            "X-FhirVault-Delivery": delivery["delivery_id"],
        }
        secret = delivery["secret"]
        if secret:
            headers["X-FhirVault-Signature"] = hmac.new(
                secret.encode("utf-8"), delivery["body"], hashlib.sha256
            ).hexdigest()
        return headers

    def _record_attempt(
        self,
        delivery: dict[str, Any],
        success: bool,
        http_status: int | None,
        error: str | None,
    ) -> None:
        attempt_number = delivery["attempts"] + 1
        outcome = "success" if success else "failure"
        moment = self.store.now()
        not_before: str | None = None
        if success:
            state = "delivered"
        elif attempt_number >= MAX_ATTEMPTS:
            state = "failed"
        else:
            state = "pending"
            not_before = (
                (self.wall_clock() + timedelta(seconds=self.delays[attempt_number - 1]))
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
        with self.store.transaction():
            connection = self.store.connection
            connection.execute(
                "INSERT INTO delivery_attempts(delivery_id, attempt, attempted_at, outcome, http_status, error) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (delivery["delivery_id"], attempt_number, moment, outcome, http_status, error),
            )
            connection.execute(
                "UPDATE deliveries SET state = ?, attempts = ?, not_before = ? WHERE delivery_id = ?",
                (state, attempt_number, not_before, delivery["delivery_id"]),
            )

    # ------------------------------------------------------------------ transport

    @staticmethod
    def _post(endpoint: str, body: bytes, headers: dict[str, str]) -> tuple[int | None, str | None]:
        request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=10) as response:
                response.read()
                return response.status, None
        except urllib.error.HTTPError as error:
            return error.code, f"endpoint responded with HTTP {error.code}"
        except (urllib.error.URLError, OSError) as error:
            reason = error.reason if isinstance(error, urllib.error.URLError) else error
            return None, str(reason) or "could not connect to the delivery endpoint"
