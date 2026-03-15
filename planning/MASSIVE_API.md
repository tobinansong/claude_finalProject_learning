# Massive API (formerly Polygon.io) — Reference for FinAlly

## Background

**Polygon.io rebranded to Massive.com** on October 30, 2025. The Python client package (`polygon-api-client`) was renamed to `massive`. All API endpoints, response shapes, and authentication mechanisms are identical — only the package name and base URL changed.

| | Before | After |
|---|---|---|
| Python package | `polygon-api-client` | `massive` |
| Base URL | `api.polygon.io` | `api.massive.com` |
| WebSocket URL | `socket.polygon.io` | `socket.massive.com` |
| Import | `from polygon import RESTClient` | `from massive import RESTClient` |

---

## Installation

```bash
# pip
pip install massive

# uv (as used in this project)
uv add massive
```

Requires Python 3.9+.

---

## Authentication

API keys are obtained from the [Massive dashboard](https://massive.com/dashboard/api-keys).

```python
from massive import RESTClient

# Option 1: Pass key directly (for development)
client = RESTClient(api_key="your_api_key_here")

# Option 2: Read from environment variable (recommended for production)
# Set MASSIVE_API_KEY in your environment, then:
client = RESTClient()   # automatically reads $MASSIVE_API_KEY
```

**Constructor parameters:**

| Parameter | Default | Purpose |
|---|---|---|
| `api_key` | `$MASSIVE_API_KEY` | Authentication token |
| `pagination` | `True` | Auto-paginate `list_*` methods |
| `trace` | `False` | Log full request/response (debugging) |
| `verbose` | `False` | Verbose logging |
| `raw` | `False` | Return raw `Response` objects instead of parsed models |

---

## Key Endpoints for FinAlly

### 1. Full Market Snapshot (Multiple Tickers) — Primary Method

**HTTP:** `GET /v2/snapshot/locale/us/markets/stocks/tickers`

Fetches live snapshot data for many tickers in a single API call. This is the endpoint used by `MassiveDataSource` for polling.

**Python — using the `massive` client:**

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key="your_key")

# Fetch snapshots for specific tickers
snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT", "TSLA"],
)

for snap in snapshots:
    print(snap.ticker)                    # "AAPL"
    print(snap.last_trade.price)          # 191.23 (float)
    print(snap.last_trade.timestamp)      # Unix timestamp (milliseconds)
    print(snap.day.open)                  # Day open price
    print(snap.day.high)                  # Day high
    print(snap.day.low)                   # Day low
    print(snap.day.close)                 # Day close (current session)
    print(snap.todays_change)             # Absolute change from prev close
    print(snap.todays_change_perc)        # % change from prev close
```

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `tickers` | list[str] | Ticker symbols to fetch. Omit for all ~10,000+ tickers. |
| `include_otc` | bool | Include OTC securities. Default: `False`. |

**Response shape (per ticker):**

```
snap.ticker                   → str      Exchange symbol (e.g., "AAPL")
snap.last_trade.price         → float    Most recent trade price
snap.last_trade.timestamp     → int      Timestamp (nanosecond precision)
snap.last_trade.size          → int      Shares in the trade
snap.last_trade.exchange      → int      Exchange code
snap.last_quote.bid           → float    Current bid price
snap.last_quote.ask           → float    Current ask price
snap.day.open                 → float    Session open
snap.day.high                 → float    Session high
snap.day.low                  → float    Session low
snap.day.close                → float    Session close (running)
snap.day.volume               → int      Session volume
snap.day.vwap                 → float    Volume-weighted average price
snap.todays_change            → float    Price change from previous close
snap.todays_change_perc       → float    Percentage change from previous close
snap.prev_day.close           → float    Previous day's closing price
```

**Timestamp note:** `last_trade.timestamp` is nanosecond precision. To convert to Unix seconds:
```python
timestamp_seconds = snap.last_trade.timestamp / 1_000_000_000
```
*(The project's `massive_client.py` divides by 1000 treating it as milliseconds — verify against live data when using a real API key.)*

---

### 2. Single Ticker Snapshot

Useful for getting a quick price check on one ticker without polling the full list.

```python
snap = client.get_snapshot_ticker(ticker="AAPL")
price = snap.last_trade.price
```

---

### 3. Previous Day Close (End-of-Day Prices)

```python
prev = client.get_previous_close(ticker="AAPL")
# prev.close  → previous day's closing price
# prev.open   → previous day's open
# prev.high   → previous day's high
# prev.low    → previous day's low
# prev.volume → previous day's volume
```

---

### 4. Historical OHLCV Bars (Aggregates)

Used for chart history, not live prices. Returns daily or intraday candle data.

```python
# Daily bars for AAPL, Jan–Jun 2024
aggs = []
for bar in client.list_aggs(
    ticker="AAPL",
    multiplier=1,
    timespan="day",       # "minute", "hour", "day", "week", "month"
    from_="2024-01-01",
    to="2024-06-30",
    limit=50000,
):
    aggs.append(bar)
    # bar.timestamp → Unix ms
    # bar.open, bar.high, bar.low, bar.close, bar.volume, bar.vwap

# Minute bars for today
for bar in client.list_aggs("AAPL", 1, "minute", "2024-06-13", "2024-06-13"):
    print(f"{bar.timestamp}: O={bar.open} H={bar.high} L={bar.low} C={bar.close}")
```

---

### 5. Last Trade and Last Quote

```python
# Most recent trade
trade = client.get_last_trade(ticker="AAPL")
print(f"Last price: ${trade.price}, Size: {trade.size}")

# Most recent bid/ask
quote = client.get_last_quote(ticker="AAPL")
print(f"Bid: ${quote.bid_price}, Ask: ${quote.ask_price}")
```

---

## Rate Limits

| Plan | Calls/Minute | Recommended Poll Interval |
|---|---|---|
| Free | 5 | 15 seconds |
| Starter | ~unlimited for REST | 5 seconds |
| Developer | ~unlimited for REST | 2 seconds |
| Advanced+ | ~unlimited for REST | 500ms |

**HTTP 429** (Too Many Requests) is returned when rate limited. **HTTP 401** means a bad or missing API key. These are distinct errors.

The free tier gives 5 calls/minute. With one `get_snapshot_all()` call per poll covering all watchlist tickers at once, the project uses **1 call per poll** regardless of watchlist size — efficient use of the rate limit.

**Free tier guidance:**
```python
# Safe for free tier (1 call per poll, 4 polls/min = 4 API calls/min)
MassiveDataSource(api_key=key, price_cache=cache, poll_interval=15.0)

# For paid tiers with higher limits
MassiveDataSource(api_key=key, price_cache=cache, poll_interval=2.0)
```

---

## Debugging

```python
# Enable request/response tracing
client = RESTClient(api_key="your_key", trace=True, verbose=True)

# This will print the full HTTP request URL and response body
snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL"],
)
```

---

## Error Handling Patterns

```python
import asyncio
import logging
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

logger = logging.getLogger(__name__)

def fetch_snapshots(client: RESTClient, tickers: list[str]) -> list:
    """Synchronous wrapper — call with asyncio.to_thread() in async code."""
    try:
        return client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=tickers,
        )
    except Exception as e:
        # Common errors:
        # - 401: Invalid API key
        # - 429: Rate limit exceeded
        # - Network errors: connection timeouts, DNS failures
        logger.error("Snapshot fetch failed: %s", e)
        return []   # Return empty; caller should retry on next interval

# Async usage (the RESTClient is synchronous — run in a thread)
async def fetch_async(client, tickers):
    return await asyncio.to_thread(fetch_snapshots, client, tickers)
```

---

## Full Working Example

```python
"""
Minimal working example: fetch live prices for a watchlist using the Massive API.
Run with: python example.py
Requires: MASSIVE_API_KEY set in environment, or pass api_key directly.
"""
import os
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

WATCHLIST = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]


def main():
    api_key = os.environ.get("MASSIVE_API_KEY")
    if not api_key:
        raise ValueError("MASSIVE_API_KEY not set")

    client = RESTClient(api_key=api_key)

    print("Fetching snapshots...")
    snapshots = client.get_snapshot_all(
        market_type=SnapshotMarketType.STOCKS,
        tickers=WATCHLIST,
    )

    print(f"{'Ticker':<8} {'Price':>10} {'Change':>10} {'Change%':>10}")
    print("-" * 42)
    for snap in snapshots:
        try:
            price = snap.last_trade.price
            change = snap.todays_change or 0
            change_pct = snap.todays_change_perc or 0
            print(f"{snap.ticker:<8} ${price:>9.2f} {change:>+10.2f} {change_pct:>+9.2f}%")
        except (AttributeError, TypeError):
            print(f"{snap.ticker:<8}  (no data)")


if __name__ == "__main__":
    main()
```

---

## How FinAlly Uses the API

The `MassiveDataSource` class (`backend/app/market/massive_client.py`) wraps this into the `MarketDataSource` interface:

1. On `start()`: creates `RESTClient`, does immediate first poll, launches polling loop
2. Every `poll_interval` seconds: calls `get_snapshot_all()` via `asyncio.to_thread()` (non-blocking)
3. For each snapshot: extracts `snap.last_trade.price` and `snap.last_trade.timestamp`, writes to `PriceCache`
4. Skips tickers with missing `last_trade` data (logs a warning)
5. On `add_ticker()`: adds to the list; new ticker appears in the next poll
6. On error: logs and continues; loop retries on next interval (no crash)

```python
# How MassiveDataSource is instantiated (via factory)
from app.market import PriceCache, create_market_data_source
import os

os.environ["MASSIVE_API_KEY"] = "your_key"

cache = PriceCache()
source = create_market_data_source(cache)  # Returns MassiveDataSource
await source.start(["AAPL", "GOOGL", "MSFT"])

# Prices now available in cache
price = cache.get_price("AAPL")
```

---

## Market Hours

Massive API provides data for:
- **Regular hours**: 9:30 AM – 4:00 PM ET (exchange prices)
- **Pre/after market**: Snapshots may reflect extended hours trades
- **Weekends/holidays**: Markets closed; `last_trade` reflects the most recent trade from the previous session

The simulator handles the "no live data" case automatically — for development without an API key, or outside market hours in production, consider using the simulator.
