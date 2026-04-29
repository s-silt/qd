"""Tests documenting asyncio.sleep timing behaviour and the inline-sleep refactor.

Background (code review P3, docs/code-review-2026-04-29.md §3.1):
  The review claimed that pre-creating an asyncio.sleep coroutine before
  blocking work causes the timer to start at *creation* time, so it would
  expire during the work and return immediately when awaited.

Empirical finding (verified 2026-04-29):
  asyncio.sleep timing starts from when the coroutine is *first awaited*,
  NOT from when the coroutine object is created.

  A pre-created `asyncio.sleep(N)` that is awaited after N+k seconds of
  other async work STILL sleeps for N seconds from that await point —
  it does NOT return immediately.

  Conclusion: the original code was behaviourally correct; the refactor to
  inline `await asyncio.sleep(...)` is a readability improvement only.
"""
import asyncio
import time
import unittest


class TestAsyncioSleepTimingBehaviour(unittest.TestCase):
    """Document the actual behaviour of asyncio.sleep with regard to timing."""

    def test_precreated_sleep_waits_full_duration_after_longer_work(self):
        """Core finding: sleep timer starts at await, not at coroutine creation.

        If sleep started at creation, elapsed would be ~0 s (already expired).
        The test asserts elapsed >= 0.03 s, proving timing starts at await.
        """
        async def _scenario():
            coro = asyncio.sleep(0.05)      # create – no timer yet
            await asyncio.sleep(0.10)       # 100 ms of work (> 50 ms sleep)
            start = time.monotonic()
            await coro                      # timer starts HERE
            return time.monotonic() - start

        elapsed = asyncio.run(_scenario())
        self.assertGreater(
            elapsed, 0.03,
            f"Pre-created sleep should still wait ~50 ms from await, "
            f"but elapsed={elapsed*1000:.1f} ms (suggests timer started at creation)",
        )

    def test_inline_sleep_waits_full_duration(self):
        """Inline await asyncio.sleep(N) at loop end waits the full N seconds."""
        async def _scenario():
            start = time.monotonic()
            await asyncio.sleep(0.05)
            return time.monotonic() - start

        elapsed = asyncio.run(_scenario())
        self.assertGreater(elapsed, 0.03,
                           f"Inline sleep should wait ~50 ms, got {elapsed*1000:.1f} ms")

    def test_precreated_and_inline_give_equivalent_loop_durations(self):
        """Both patterns produce the same total loop duration.

        This confirms the inline refactor is behaviour-preserving.
        """
        interval = 0.05
        work_dur = 0.02

        async def _pre_created_pattern():
            coro = asyncio.sleep(interval)
            await asyncio.sleep(work_dur)   # simulate work
            await coro

        async def _inline_pattern():
            await asyncio.sleep(work_dur)   # simulate work
            await asyncio.sleep(interval)   # sleep at end

        t1_start = time.monotonic()
        asyncio.run(_pre_created_pattern())
        t1 = time.monotonic() - t1_start

        t2_start = time.monotonic()
        asyncio.run(_inline_pattern())
        t2 = time.monotonic() - t2_start

        self.assertAlmostEqual(
            t1, t2, delta=0.03,
            msg=(
                f"pre-created={t1:.3f}s vs inline={t2:.3f}s — "
                "patterns should take approximately the same time"
            ),
        )

    def test_sleep_zero_yields_without_significant_delay(self):
        """asyncio.sleep(0) yields control but returns essentially instantly."""
        async def _scenario():
            start = time.monotonic()
            await asyncio.sleep(0)
            return time.monotonic() - start

        elapsed = asyncio.run(_scenario())
        self.assertLess(elapsed, 0.02,
                        f"asyncio.sleep(0) should return quickly, got {elapsed*1000:.1f} ms")

    def test_review_claim_is_false_sleep_does_not_expire_during_creation(self):
        """Explicitly assert the review claim is incorrect.

        The review said: "asyncio.sleep() starts timing from when the coroutine
        is *created*, not when it is first awaited."  This test shows that claim
        is false: even when work (100 ms) far exceeds the sleep (50 ms), the
        await still blocks for ~50 ms, not 0 ms.
        """
        async def _scenario():
            coro = asyncio.sleep(0.05)          # 50 ms sleep
            await asyncio.sleep(0.10)           # 100 ms work — more than the sleep
            before = time.monotonic()
            await coro
            after = time.monotonic()
            return after - before

        elapsed = asyncio.run(_scenario())
        # If review were right: elapsed ≈ 0  (sleep already "expired")
        # Actual behaviour:    elapsed ≈ 0.05 (sleep starts from await)
        self.assertGreater(
            elapsed, 0.03,
            "Review claim is false: sleep did NOT expire during pre-await work",
        )


if __name__ == '__main__':
    unittest.main()
