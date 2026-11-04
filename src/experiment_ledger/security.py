"""Password hashing (bcrypt), JWT issuing/verification and a login rate limiter."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from experiment_ledger.models import Role

# bcrypt only uses the first 72 bytes; reject longer inputs instead of silently truncating.
MAX_PASSWORD_BYTES = 72

# A fixed, valid hash used to equalize timing when the email doesn't exist.
_DUMMY_HASH = bcrypt.hashpw(b"timing-equalizer", bcrypt.gensalt(rounds=12))


def hash_password(password: str) -> str:
    raw = password.encode()
    if len(raw) > MAX_PASSWORD_BYTES:
        raise ValueError("password too long")
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, password_hash: str | None) -> bool:
    raw = password.encode()[:MAX_PASSWORD_BYTES]
    if password_hash is None:
        bcrypt.checkpw(raw, _DUMMY_HASH)
        return False
    return bcrypt.checkpw(raw, password_hash.encode())


def create_access_token(
    *, user_id: int, role: Role, secret: str, algorithm: str, minutes: int
) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "role": role.value,
        "iat": now,
        "exp": now + timedelta(minutes=minutes),
    }
    return jwt.encode(payload, secret, algorithm=algorithm)


class InvalidToken(Exception):
    pass


def decode_access_token(token: str, *, secret: str, algorithm: str) -> tuple[int, Role]:
    try:
        payload = jwt.decode(
            token, secret, algorithms=[algorithm], options={"require": ["exp", "sub", "role"]}
        )
        return int(payload["sub"]), Role(payload["role"])
    except (jwt.PyJWTError, ValueError, KeyError) as exc:
        raise InvalidToken from exc


class SlidingWindowRateLimiter:
    """In-process sliding-window limiter. Adequate for a single instance; swap for Redis when
    running multiple replicas (see README roadmap)."""

    def __init__(self, max_events: int, window_seconds: int) -> None:
        self.max_events = max_events
        self.window = window_seconds
        self._events: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        with self._lock:
            events = self._events[key]
            while events and current - events[0] >= self.window:
                events.popleft()
            if len(events) >= self.max_events:
                return False
            events.append(current)
            return True

    def retry_after(self, key: str, now: float | None = None) -> int:
        current = time.monotonic() if now is None else now
        with self._lock:
            events = self._events.get(key)
            if not events:
                return 0
            return max(1, int(self.window - (current - events[0])) + 1)
