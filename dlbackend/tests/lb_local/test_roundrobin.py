"""Tests for the RoundRobin class."""

import threading

import pytest
from fastapi import HTTPException

from lbserver.utils import RoundRobin


class TestRoundRobin:
    def test_cycles_through_backends(self):
        rr = RoundRobin(["a", "b", "c"])
        assert rr.next() == "a"
        assert rr.next() == "b"
        assert rr.next() == "c"
        assert rr.next() == "a"

    def test_single_backend(self):
        rr = RoundRobin(["only"])
        for _ in range(5):
            assert rr.next() == "only"

    def test_empty_backends_raises_503(self):
        rr = RoundRobin([])
        with pytest.raises(HTTPException) as exc_info:
            rr.next()
        assert exc_info.value.status_code == 503

    def test_thread_safety(self):
        backends = ["a", "b", "c"]
        rr = RoundRobin(backends)
        results: list[str] = []
        lock = threading.Lock()

        def pick(n: int) -> None:
            for _ in range(n):
                val = rr.next()
                with lock:
                    results.append(val)

        threads = [threading.Thread(target=pick, args=(100,)) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == 1000
        assert set(results) == set(backends)
