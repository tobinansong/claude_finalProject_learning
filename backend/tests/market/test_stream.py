"""Tests for the SSE streaming endpoint and event generator."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from app.market.cache import PriceCache
from app.market.stream import _generate_events, create_stream_router


def _mock_request(disconnect_after: int = 5) -> MagicMock:
    """Create a mock Request that disconnects after N checks."""
    request = MagicMock()
    request.client = MagicMock()
    request.client.host = "127.0.0.1"

    call_count = 0

    async def is_disconnected():
        nonlocal call_count
        call_count += 1
        return call_count > disconnect_after

    request.is_disconnected = is_disconnected
    return request


async def _collect_events(
    cache: PriceCache,
    request: MagicMock,
    interval: float = 0.01,
    keepalive_interval: float = 15.0,
) -> list[str]:
    """Collect all events from the generator until it stops."""
    events = []
    async for event in _generate_events(cache, request, interval, keepalive_interval):
        events.append(event)
    return events


@pytest.mark.asyncio
class TestSSEGenerator:
    """Tests for the _generate_events async generator."""

    async def test_first_event_is_retry_directive(self):
        """Test that the first event is the retry directive."""
        cache = PriceCache()
        request = _mock_request(disconnect_after=1)

        events = await _collect_events(cache, request)
        assert events[0] == "retry: 1000\n\n"

    async def test_sends_price_data_as_json(self):
        """Test that price data is sent as SSE data events."""
        cache = PriceCache()
        cache.update("AAPL", 190.50)
        request = _mock_request(disconnect_after=2)

        events = await _collect_events(cache, request)

        data_events = [e for e in events if e.startswith("data:")]
        assert len(data_events) >= 1

        payload = json.loads(data_events[0][len("data:"):].strip())
        assert "AAPL" in payload
        assert payload["AAPL"]["price"] == 190.50
        assert payload["AAPL"]["ticker"] == "AAPL"

    async def test_sends_multiple_tickers(self):
        """Test that all cached tickers are included in the event."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.update("GOOGL", 175.00)
        cache.update("MSFT", 420.00)
        request = _mock_request(disconnect_after=2)

        events = await _collect_events(cache, request)

        data_events = [e for e in events if e.startswith("data:")]
        payload = json.loads(data_events[0][len("data:"):].strip())
        assert set(payload.keys()) == {"AAPL", "GOOGL", "MSFT"}

    async def test_direction_field_in_payload(self):
        """Test that direction (up/down/flat) is included in payload."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.update("AAPL", 195.00)  # price went up
        request = _mock_request(disconnect_after=2)

        events = await _collect_events(cache, request)

        data_events = [e for e in events if e.startswith("data:")]
        payload = json.loads(data_events[0][len("data:"):].strip())
        assert payload["AAPL"]["direction"] == "up"

    async def test_no_data_event_when_cache_empty(self):
        """Test that no data event is sent when cache is empty."""
        cache = PriceCache()
        request = _mock_request(disconnect_after=3)

        events = await _collect_events(cache, request)

        data_events = [e for e in events if e.startswith("data:")]
        assert len(data_events) == 0

    async def test_version_based_dedup(self):
        """Test that events are only sent when cache version changes."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        request = _mock_request(disconnect_after=5)

        events = await _collect_events(cache, request)

        # Only one data event should be sent (version doesn't change between polls)
        data_events = [e for e in events if e.startswith("data:")]
        assert len(data_events) == 1

    async def test_stops_on_disconnect(self):
        """Test that the generator stops when client disconnects."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        request = _mock_request(disconnect_after=3)

        events = await _collect_events(cache, request)

        # Should have retry + some data, but should terminate
        assert len(events) >= 1
        assert events[0] == "retry: 1000\n\n"

    async def test_keepalive_sent_when_idle(self):
        """Test that keepalive comments are sent when no data changes."""
        cache = PriceCache()
        # Empty cache — no data events, only keepalives
        request = _mock_request(disconnect_after=200)

        events = await _collect_events(
            cache, request, interval=0.01, keepalive_interval=0.05
        )

        keepalives = [e for e in events if e == ": keepalive\n\n"]
        assert len(keepalives) >= 1

    async def test_keepalive_not_sent_when_data_flowing(self):
        """Test that keepalives aren't sent when data events are being sent."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        # Only 2 polls — version sent on first, no time for keepalive
        request = _mock_request(disconnect_after=2)

        events = await _collect_events(
            cache, request, interval=0.01, keepalive_interval=15.0
        )

        keepalives = [e for e in events if e == ": keepalive\n\n"]
        assert len(keepalives) == 0

    async def test_payload_contains_all_fields(self):
        """Test that each ticker payload contains all expected fields."""
        cache = PriceCache()
        cache.update("AAPL", 190.00, timestamp=1234567890.0)
        cache.update("AAPL", 191.50, timestamp=1234567891.0)
        request = _mock_request(disconnect_after=2)

        events = await _collect_events(cache, request)

        data_events = [e for e in events if e.startswith("data:")]
        payload = json.loads(data_events[0][len("data:"):].strip())
        aapl = payload["AAPL"]

        expected_keys = {"ticker", "price", "previous_price", "timestamp",
                         "change", "change_percent", "direction"}
        assert set(aapl.keys()) == expected_keys


class TestCreateStreamRouter:
    """Tests for the router factory function."""

    def test_creates_router_with_prices_endpoint(self):
        """Test that the factory creates a router with the /prices route."""
        cache = PriceCache()
        router = create_stream_router(cache)

        route_paths = [route.path for route in router.routes]
        assert any("/prices" in path for path in route_paths)

    def test_creates_fresh_router_each_call(self):
        """Test that each call creates a new router (no shared state)."""
        cache1 = PriceCache()
        cache2 = PriceCache()

        router1 = create_stream_router(cache1)
        router2 = create_stream_router(cache2)

        assert router1 is not router2
