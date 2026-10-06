"""Sampling policy: keep representative traces, never drop errors."""
from __future__ import annotations

import random
import threading


class SamplingPolicy:
    """Rate-based sampler; errors and slow requests are always kept.

    The rate may be changed at runtime; the change takes effect for
    subsequent decisions only, so behaviour is predictable.
    """

    def __init__(self, rate: float = 1.0) -> None:
        self._lock = threading.Lock()
        self._rate = 1.0
        self.set_rate(rate)

    @property
    def rate(self) -> float:
        with self._lock:
            return self._rate

    def set_rate(self, rate: float) -> None:
        if not 0.0 <= rate <= 1.0:
            raise ValueError(f"sample rate must be in [0, 1], got {rate}")
        with self._lock:
            self._rate = rate

    def decide(self) -> bool:
        """Pre-decide, at request start, whether a request is logged to the
        main log assuming it turns out neither erroneous nor slow.

        Errors and slow requests cannot be known yet at request start, so
        the final keep/drop is ``is_error or is_slow or decide()``; the
        pre-decision only routes segments of *normal* requests. The draw
        is made once per request and stored on its trace context, so all
        segments of one request (entry, downstream calls, background
        tasks) are routed consistently.
        """
        rate = self.rate
        if rate >= 1.0:
            return True
        if rate <= 0.0:
            return False
        return random.random() < rate

    def should_keep(self, *, is_error: bool, is_slow: bool) -> bool:
        if is_error or is_slow:
            return True
        return self.decide()


sampling_policy = SamplingPolicy()
