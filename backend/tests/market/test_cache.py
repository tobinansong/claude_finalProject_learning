"""Tests for PriceCache."""

import threading

from app.market.cache import PriceCache


class TestPriceCache:
    """Unit tests for the PriceCache."""

    def test_update_and_get(self):
        """Test updating and getting a price."""
        cache = PriceCache()
        update = cache.update("AAPL", 190.50)
        assert update.ticker == "AAPL"
        assert update.price == 190.50
        assert cache.get("AAPL") == update

    def test_first_update_is_flat(self):
        """Test that the first update has flat direction."""
        cache = PriceCache()
        update = cache.update("AAPL", 190.50)
        assert update.direction == "flat"
        assert update.previous_price == 190.50

    def test_direction_up(self):
        """Test price update with upward direction."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        update = cache.update("AAPL", 191.00)
        assert update.direction == "up"
        assert update.change == 1.00

    def test_direction_down(self):
        """Test price update with downward direction."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        update = cache.update("AAPL", 189.00)
        assert update.direction == "down"
        assert update.change == -1.00

    def test_remove(self):
        """Test removing a ticker from cache."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.remove("AAPL")
        assert cache.get("AAPL") is None

    def test_remove_nonexistent(self):
        """Test removing a ticker that doesn't exist."""
        cache = PriceCache()
        cache.remove("AAPL")  # Should not raise

    def test_get_all(self):
        """Test getting all prices."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.update("GOOGL", 175.00)
        all_prices = cache.get_all()
        assert set(all_prices.keys()) == {"AAPL", "GOOGL"}

    def test_version_increments(self):
        """Test that version counter increments."""
        cache = PriceCache()
        v0 = cache.version
        cache.update("AAPL", 190.00)
        assert cache.version == v0 + 1
        cache.update("AAPL", 191.00)
        assert cache.version == v0 + 2

    def test_get_price_convenience(self):
        """Test the convenience get_price method."""
        cache = PriceCache()
        cache.update("AAPL", 190.50)
        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("NOPE") is None

    def test_len(self):
        """Test __len__ method."""
        cache = PriceCache()
        assert len(cache) == 0
        cache.update("AAPL", 190.00)
        assert len(cache) == 1
        cache.update("GOOGL", 175.00)
        assert len(cache) == 2

    def test_contains(self):
        """Test __contains__ method."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        assert "AAPL" in cache
        assert "GOOGL" not in cache

    def test_custom_timestamp(self):
        """Test updating with a custom timestamp."""
        cache = PriceCache()
        custom_ts = 1234567890.0
        update = cache.update("AAPL", 190.50, timestamp=custom_ts)
        assert update.timestamp == custom_ts

    def test_price_rounding(self):
        """Test that prices are rounded to 2 decimal places."""
        cache = PriceCache()
        update = cache.update("AAPL", 190.12345)
        assert update.price == 190.12

    def test_zero_timestamp_preserved(self):
        """Test that timestamp=0.0 is not discarded (regression for 'or' bug)."""
        cache = PriceCache()
        update = cache.update("AAPL", 190.00, timestamp=0.0)
        assert update.timestamp == 0.0

    def test_remove_increments_version(self):
        """Test that remove() increments the version counter."""
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        v_before = cache.version
        cache.remove("AAPL")
        assert cache.version == v_before + 1

    def test_remove_nonexistent_does_not_increment_version(self):
        """Test that removing a non-existent ticker does not change version."""
        cache = PriceCache()
        v_before = cache.version
        cache.remove("NOPE")
        assert cache.version == v_before

    def test_thread_safety(self):
        """Test that concurrent updates from multiple threads don't corrupt state."""
        cache = PriceCache()
        num_threads = 8
        updates_per_thread = 500
        barrier = threading.Barrier(num_threads)

        def writer(thread_id: int) -> None:
            barrier.wait()  # Synchronise all threads to start together
            ticker = f"T{thread_id}"
            for i in range(updates_per_thread):
                cache.update(ticker, 100.0 + i * 0.01)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Each thread wrote a unique ticker, all should be present
        assert len(cache) == num_threads
        # Version should equal total number of updates
        assert cache.version == num_threads * updates_per_thread
        # Each ticker should have its final price
        for i in range(num_threads):
            update = cache.get(f"T{i}")
            assert update is not None
            assert update.price == round(100.0 + (updates_per_thread - 1) * 0.01, 2)
