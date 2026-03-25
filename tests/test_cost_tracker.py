"""
Unit tests for CallCostTracker.

Pure unit tests — no I/O, no mocking needed except for the Redis persist() method.
"""
import json
import pytest

from app.logging_analytics.cost_tracker import (
    CallCostTracker,
    _DG_COST_PER_SEC,
    _OAI_INPUT_COST_PER_TOKEN,
    _OAI_OUTPUT_COST_PER_TOKEN,
    _EL_COST_PER_CHAR,
)


class TestCallCostTrackerAccumulators:
    def setup_method(self):
        self.tracker = CallCostTracker("CA_test_123")

    def test_initial_state_all_zeros(self):
        assert self.tracker._deepgram_seconds == 0.0
        assert self.tracker._openai_input_tokens == 0
        assert self.tracker._openai_output_tokens == 0
        assert self.tracker._elevenlabs_chars == 0

    def test_add_deepgram_seconds(self):
        self.tracker.add_deepgram_seconds(30.0)
        assert self.tracker._deepgram_seconds == 30.0

    def test_add_deepgram_seconds_accumulates(self):
        self.tracker.add_deepgram_seconds(10.0)
        self.tracker.add_deepgram_seconds(20.0)
        assert self.tracker._deepgram_seconds == 30.0

    def test_add_openai_tokens(self):
        self.tracker.add_openai_tokens(input=500, output=100)
        assert self.tracker._openai_input_tokens == 500
        assert self.tracker._openai_output_tokens == 100

    def test_add_openai_tokens_accumulates(self):
        self.tracker.add_openai_tokens(input=200, output=50)
        self.tracker.add_openai_tokens(input=300, output=50)
        assert self.tracker._openai_input_tokens == 500
        assert self.tracker._openai_output_tokens == 100

    def test_add_elevenlabs_chars(self):
        self.tracker.add_elevenlabs_chars(500)
        assert self.tracker._elevenlabs_chars == 500

    def test_add_elevenlabs_chars_accumulates(self):
        self.tracker.add_elevenlabs_chars(200)
        self.tracker.add_elevenlabs_chars(300)
        assert self.tracker._elevenlabs_chars == 500

    def test_negative_values_are_clamped_to_zero(self):
        self.tracker.add_deepgram_seconds(-10.0)
        self.tracker.add_openai_tokens(input=-100, output=-50)
        self.tracker.add_elevenlabs_chars(-200)
        assert self.tracker._deepgram_seconds == 0.0
        assert self.tracker._openai_input_tokens == 0
        assert self.tracker._openai_output_tokens == 0
        assert self.tracker._elevenlabs_chars == 0


class TestCallCostTrackerCalculations:
    def test_deepgram_cost(self):
        tracker = CallCostTracker("CA1")
        tracker.add_deepgram_seconds(60.0)
        expected = 60.0 * _DG_COST_PER_SEC
        assert tracker.deepgram_usd() == pytest.approx(expected, rel=1e-6)

    def test_openai_cost(self):
        tracker = CallCostTracker("CA2")
        tracker.add_openai_tokens(input=1_000_000, output=0)
        assert tracker.openai_usd() == pytest.approx(2.50, rel=1e-4)

    def test_openai_output_cost(self):
        tracker = CallCostTracker("CA3")
        tracker.add_openai_tokens(input=0, output=1_000_000)
        assert tracker.openai_usd() == pytest.approx(10.00, rel=1e-4)

    def test_elevenlabs_cost(self):
        tracker = CallCostTracker("CA4")
        tracker.add_elevenlabs_chars(1000)
        assert tracker.elevenlabs_usd() == pytest.approx(0.30, rel=1e-4)

    def test_total_is_sum(self):
        tracker = CallCostTracker("CA5")
        tracker.add_deepgram_seconds(60.0)
        tracker.add_openai_tokens(input=500, output=100)
        tracker.add_elevenlabs_chars(300)
        expected = (
            tracker.deepgram_usd() + tracker.openai_usd() + tracker.elevenlabs_usd()
        )
        assert tracker.total_usd() == pytest.approx(expected, rel=1e-6)

    def test_total_is_rounded_to_6_decimal_places(self):
        tracker = CallCostTracker("CA6")
        tracker.add_deepgram_seconds(17.333)
        total = tracker.total_usd()
        # Should not have more than 6 decimal places
        assert round(total, 6) == total

    def test_zero_usage_zero_cost(self):
        tracker = CallCostTracker("CA7")
        assert tracker.total_usd() == 0.0


class TestCallCostTrackerBreakdown:
    def test_breakdown_keys(self):
        tracker = CallCostTracker("CA8")
        tracker.add_deepgram_seconds(10.0)
        tracker.add_openai_tokens(input=100, output=50)
        tracker.add_elevenlabs_chars(200)
        bd = tracker.breakdown()
        expected_keys = {
            "deepgram_seconds", "openai_input_tokens", "openai_output_tokens",
            "elevenlabs_chars", "deepgram_usd", "openai_usd", "elevenlabs_usd", "total_usd"
        }
        assert set(bd.keys()) == expected_keys

    def test_breakdown_total_matches_total_usd(self):
        tracker = CallCostTracker("CA9")
        tracker.add_deepgram_seconds(30.0)
        tracker.add_openai_tokens(input=1000, output=200)
        tracker.add_elevenlabs_chars(500)
        bd = tracker.breakdown()
        assert bd["total_usd"] == tracker.total_usd()


class TestCallCostTrackerPersist:
    async def test_persist_pushes_to_db_sync_queue(self):
        """persist() should rpush the expected payload to db_sync_queue."""
        calls: list = []

        class FakeRedis:
            async def rpush(self, key, value):
                calls.append((key, value))

        tracker = CallCostTracker("CA_persist_test")
        tracker.add_deepgram_seconds(60.0)
        tracker.add_elevenlabs_chars(400)

        await tracker.persist(FakeRedis())

        assert len(calls) == 1
        key, value = calls[0]
        assert key == "db_sync_queue"
        data = json.loads(value)
        assert data["type"] == "call_cost"
        assert data["call_sid"] == "CA_persist_test"
        assert data["cost_usd"] == tracker.total_usd()
        assert "breakdown" in data

    async def test_persist_logs_error_on_redis_failure(self, caplog):
        """persist() should catch Redis errors without raising."""
        class BrokenRedis:
            async def rpush(self, *args, **kwargs):
                raise ConnectionError("Redis down")

        tracker = CallCostTracker("CA_broken")
        tracker.add_deepgram_seconds(10.0)

        import logging
        with caplog.at_level(logging.ERROR, logger="app.logging_analytics.cost_tracker"):
            await tracker.persist(BrokenRedis())  # must not raise

        assert any("Cost persist failed" in r.message for r in caplog.records)
