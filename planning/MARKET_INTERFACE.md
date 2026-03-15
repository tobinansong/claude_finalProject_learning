# Market Data Interface — Unified Python API

## Overview

The market data subsystem provides a **single interface** for retrieving live stock prices, regardless of whether the data comes from the Massive (Polygon.io) REST API or the built-in simulator. Downstream code — SSE streaming, portfolio valuation, trade execution — never knows which source is active.

```
┌─────────────────────────────────────────────────────┐
│                  MarketDataSource (ABC)              │
│                                                     │
│   SimulatorDataSource          MassiveDataSource    │
│   (default, no key needed)     (MASSIVE_API_KEY set)│
└──────────────────┬──────────────────────────────────┘
                   │ writes to
                   ▼
           ┌───────────────┐
           │  PriceCache   │  (thread-safe, in-memory)
           └───────┬───────┘
                   │ read by
        ┌──────────┼──────────┐
        ▼          ▼          ▼
    SSE stream  Portfolio  Trade
    endpoint    valuation  execution
```

---

## Module Layout

```
backend/app/market/
├── __init__.py          # Public API exports
├── models.py            # PriceUpdate dataclass
├── interface.py         # MarketDataSource abstract base class
├── cache.py             # PriceCache — thread-safe price store
├── seed_prices.py       # Seed prices and GBM params for default tickers
├── simulator.py         # GBMSimulator + SimulatorDataSource
├── massive_client.py    # MassiveDataSource (Polygon.io REST poller)
├── factory.py           # create_market_data_source() — env-driven selection
└── stream.py            # FastAPI SSE router factory
```

---

## Core Types

### `PriceUpdate` — `models.py`

Immutable, frozen dataclass representing a single price snapshot.

```python
from dataclasses import dataclass, field
import time

@dataclass(frozen=True, slots=True)
class PriceUpdate:
    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float: ...          # Absolute change
    @property
    def change_percent(self) -> float: ...  # % change
    @property
    def direction(self) -> str: ...         # "up" | "down" | "flat"

    def to_dict(self) -> dict: ...          # JSON-serializable dict for SSE
```

**`to_dict()` output** (sent in every SSE event):
```json
{
  "ticker": "AAPL",
  "price": 191.23,
  "previous_price": 190.88,
  "timestamp": 1710512345.678,
  "change": 0.35,
  "change_percent": 0.1833,
  "direction": "up"
}
```

---

### `PriceCache` — `cache.py`

Thread-safe in-memory store. One writer (the active data source), multiple readers (SSE, portfolio).

```python
class PriceCache:
    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate
    def get(self, ticker: str) -> PriceUpdate | None
    def get_price(self, ticker: str) -> float | None
    def get_all(self) -> dict[str, PriceUpdate]   # shallow copy
    def remove(self, ticker: str) -> None
    @property
    def version(self) -> int   # monotonic counter, increments on every update
```

**Usage examples:**
```python
cache = PriceCache()

# Write (done by data source internally)
update = cache.update("AAPL", 191.23)

# Read current price
price = cache.get_price("AAPL")          # 191.23 or None

# Read full snapshot (for SSE payload)
all_prices = cache.get_all()             # {"AAPL": PriceUpdate(...), ...}

# Version-based change detection (used by SSE to avoid redundant sends)
if cache.version != last_known_version:
    send_sse_event()
```

**Thread safety:** All reads and writes are protected by a `threading.Lock`. The data source runs in an asyncio task and uses `asyncio.to_thread()` for any synchronous blocking calls (Massive API) before writing to the cache.

---

### `MarketDataSource` — `interface.py`

Abstract base class. Both implementations must satisfy this contract.

```python
from abc import ABC, abstractmethod

class MarketDataSource(ABC):

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates. Launches background task."""

    @abstractmethod
    async def stop(self) -> None:
        """Cancel background task and release resources. Safe to call multiple times."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. Included in next update cycle."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker. Also evicts it from PriceCache."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Current list of tracked tickers."""
```

**Lifecycle:**
```python
# Application startup
cache = PriceCache()
source = create_market_data_source(cache)
await source.start(["AAPL", "GOOGL", "MSFT", ...])

# Dynamic watchlist management (called from API route handlers)
await source.add_ticker("TSLA")
await source.remove_ticker("GOOGL")

# Application shutdown (FastAPI lifespan handler)
await source.stop()
```

---

## Factory — `factory.py`

Selects the implementation based on the `MASSIVE_API_KEY` environment variable.

```python
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        return SimulatorDataSource(price_cache=price_cache)
```

| `MASSIVE_API_KEY` | Source selected | Data type |
|---|---|---|
| Not set / empty | `SimulatorDataSource` | Simulated (GBM) |
| Set and non-empty | `MassiveDataSource` | Real (Polygon.io) |

---

## SSE Streaming — `stream.py`

A FastAPI router factory that exposes `GET /api/stream/prices` as an SSE endpoint.

```python
from app.market import create_stream_router

router = create_stream_router(price_cache)
app.include_router(router)
```

**Endpoint behavior:**
- Long-lived `text/event-stream` connection
- Emits all tracked prices every ~500ms when the cache version changes
- Sends `retry: 1000` directive so `EventSource` auto-reconnects after 1 second
- Detects client disconnect via `request.is_disconnected()`
- Sets `X-Accel-Buffering: no` to disable nginx proxy buffering

**SSE event format:**
```
data: {"AAPL": {"ticker":"AAPL","price":191.23,...}, "MSFT": {...}, ...}

```
(All tickers in one JSON object per event, not individual events per ticker)

**Client (JavaScript):**
```javascript
const es = new EventSource('/api/stream/prices');
es.onmessage = (event) => {
  const prices = JSON.parse(event.data);
  // prices["AAPL"].price, prices["AAPL"].direction, etc.
};
```

---

## Full Integration Example

```python
# backend/app/main.py (FastAPI application startup)
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.market import PriceCache, create_market_data_source, create_stream_router

DEFAULT_TICKERS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
                   "NVDA", "META", "JPM", "V", "NFLX"]

price_cache = PriceCache()
market_source = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global market_source
    market_source = create_market_data_source(price_cache)
    await market_source.start(DEFAULT_TICKERS)
    yield
    await market_source.stop()

app = FastAPI(lifespan=lifespan)
app.include_router(create_stream_router(price_cache))

# API route: add ticker to watchlist
@app.post("/api/watchlist")
async def add_ticker(body: dict):
    ticker = body["ticker"].upper()
    await market_source.add_ticker(ticker)
    return {"ticker": ticker, "price": price_cache.get_price(ticker)}

# API route: get current prices for portfolio valuation
@app.get("/api/portfolio")
async def get_portfolio():
    prices = price_cache.get_all()
    # Use prices[ticker].price to value each position
    ...
```

---

## Design Decisions

| Decision | Rationale |
|---|---|
| **Abstract base class** | Strategy pattern — callers bind to the interface, not the implementation. Switching sources requires zero changes to downstream code. |
| **PriceCache as intermediary** | Decouples producers (polling/simulation loops) from consumers (SSE, portfolio). Multiple readers never contend with writes. |
| **Write once, read many** | Only one data source runs at a time; many API handlers and SSE connections can read simultaneously with no coordination. |
| **Version counter on cache** | SSE generator polls every 500ms but only emits when version changes — avoids sending duplicate data, saves bandwidth. |
| **Async interface, sync internals** | The `massive` RESTClient is synchronous. `asyncio.to_thread()` wraps it so the event loop is never blocked. The simulator is pure Python math — runs in the event loop directly. |
| **`asyncio.to_thread()` for blocking calls** | Keeps the single FastAPI event loop free. The GBM step is so fast (microseconds) it doesn't need to be threaded. |
