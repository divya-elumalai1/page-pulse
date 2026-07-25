"""
Per-client token-bucket rate limiter.

Single-process, in-memory -- good enough for this task's scale. At
production scale (Task B) this state would live in Redis so it's shared
across horizontally-scaled instances.
"""
import time
import threading


class TokenBucket:
    def __init__(self, capacity: int, refill_per_second: float):
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self.tokens = capacity
        self.last_refill = time.monotonic()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
        self.last_refill = now

    def consume(self, amount: int = 1) -> bool:
        self._refill()
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False


class RateLimiter:
    def __init__(self, requests_per_minute: int = 30):
        self.requests_per_minute = requests_per_minute
        self.refill_per_second = requests_per_minute / 60.0
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def allow(self, client_id: str) -> bool:
        with self._lock:
            bucket = self._buckets.get(client_id)
            if bucket is None:
                bucket = TokenBucket(self.requests_per_minute, self.refill_per_second)
                self._buckets[client_id] = bucket
            return bucket.consume(1)
