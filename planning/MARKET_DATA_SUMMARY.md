# Market Data Backend — Summary

**Status:** Complete, tested, all code-review issues resolved.
**Test suite:** 94 tests, all passing. Overall coverage: ~87%.

---

## Architecture

```
MarketDataSource (ABC)
├── SimulatorDataSource  →  GBM simulator (default, no API key needed)
└── MassiveDataSource    →  Polygon.io REST poller (when MASSIVE_API_KEY set)
        │
        ▼
   PriceCache (thread-safe, in-memory, version-stamped)
        │
        ├──→ SSE endpoint  GET /api/stream/prices
        ├──→ Portfolio valuation
        └──→ Trade execution
```

All downstream code (SSE streaming, portfolio valuation, trade execution) is source-agnostic — it only reads from `PriceCache`. This is the strategy pattern: swap the data source by setting `MASSIVE_API_KEY`; nothing else changes.

---

## Modules (`backend/app/market/`)

| File | Exports | Purpose |
|------|---------|---------|
| `models.py` | `PriceUpdate` | Immutable frozen dataclass: `ticker`, `price`, `previous_price`, `timestamp`, computed `change`, `change_percent`, `direction`, `to_dict()` |
| `cache.py` | `PriceCache` | Thread-safe dict protected by `threading.Lock`; monotonic `version` counter increments on every write (update or remove) for SSE change detection |
| `interface.py` | `MarketDataSource` | Abstract base class: `start(tickers)`, `stop()`, `add_ticker(ticker)`, `remove_ticker(ticker)`, `get_tickers()` |
| `seed_prices.py` | `SEED_PRICES`, `TICKER_PARAMS`, `DEFAULT_PARAMS`, `CORRELATION_GROUPS` | Realistic starting prices and per-ticker GBM parameters (drift `mu`, volatility `sigma`) for AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX |
| `simulator.py` | `GBMSimulator`, `SimulatorDataSource` | GBM price engine with Cholesky-correlated moves; runs as an asyncio background task at 500ms intervals |
| `massive_client.py` | `MassiveDataSource` | Polls Polygon.io REST API at a configurable interval; normalises tickers to uppercase |
| `factory.py` | `create_market_data_source` | Returns `MassiveDataSource` if `MASSIVE_API_KEY` env var is set, otherwise `SimulatorDataSource`; imports are lazy |
| `stream.py` | `create_stream_router` | FastAPI SSE endpoint factory; version-based change detection; keepalive comments every 15s to prevent proxy timeouts |

---

## PriceCache

```python
cache = PriceCache()

# Write
update = cache.update("AAPL", 190.50)            # returns PriceUpdate
cache.remove("AAPL")                              # bumps version

# Read
update = cache.get("AAPL")       # PriceUpdate | None
price  = cache.get_price("AAPL") # float | None
all_   = cache.get_all()         # dict[str, PriceUpdate] (shallow copy)

# SSE change detection
v = cache.version   # int, increments on every update or remove
len(cache)          # number of tracked tickers
"AAPL" in cache     # bool
```

`PriceUpdate` properties:
- `change` — `price - previous_price`
- `change_percent` — `change / previous_price * 100` (0 if previous_price is 0)
- `direction` — `"up"` | `"down"` | `"flat"`
- `to_dict()` — JSON-serialisable dict (all fields)

---

## GBM Simulator

Geometric Brownian Motion:

```
S(t+dt) = S(t) * exp((mu - 0.5*sigma²)*dt + sigma*sqrt(dt)*Z)
```

- `dt` = 0.5s / 5,896,800s (a trading year) ≈ 8.5e-8
- `Z` = correlated standard normal draw via Cholesky decomposition
- **Correlation structure**: tech stocks 0.6, finance stocks 0.5, cross-sector 0.3, TSLA 0.3 (does its own thing)
- **Random shock events**: ~0.1% chance per tick per ticker of a 2–5% move for visual drama
- Starts from realistic seed prices (AAPL ~$190, NVDA ~$800, etc.)

---

## Massive API Client

- Polls Polygon.io REST `/v2/snapshot/locale/us/markets/stocks/tickers` endpoint
- Parses `results[].lastTrade.p` (last trade price) and `results[].updated` (epoch ms timestamp)
- Skips malformed snapshots gracefully (logs a warning, continues)
- Poll interval configurable; defaults: free tier 15s, paid tier 2s
- All tickers normalised to uppercase on `add_ticker` / `remove_ticker`

---

## SSE Endpoint

**`GET /api/stream/prices`** — `text/event-stream`

First message: `retry: 1000\n\n` (browser auto-reconnects after 1s on drop).

Subsequent messages:
```
data: {"AAPL": {"ticker": "AAPL", "price": 190.50, "previous_price": 190.00,
        "timestamp": 1234567890.0, "change": 0.50, "change_percent": 0.26,
        "direction": "up"}, ...}
```

- Sends an update only when `cache.version` changes (no redundant events)
- Sends `: keepalive` comment every 15s of idle to prevent proxy timeouts
- `Cache-Control: no-cache`, `X-Accel-Buffering: no` headers set

Usage in FastAPI:
```python
from app.market import create_stream_router

router = create_stream_router(price_cache)   # Fresh APIRouter, no shared state
app.include_router(router)
```

---

## Factory & Lifecycle

```python
from app.market import PriceCache, create_market_data_source

# Application startup
cache = PriceCache()
source = create_market_data_source(cache)   # reads MASSIVE_API_KEY env var
await source.start(["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
                    "NVDA", "META", "JPM", "V", "NFLX"])

# Runtime — watchlist changes
await source.add_ticker("PYPL")     # case-normalised internally
await source.remove_ticker("NFLX")

# Read prices (from any async or sync context)
update    = cache.get("AAPL")       # PriceUpdate | None
price     = cache.get_price("AAPL") # float | None
all_prices = cache.get_all()        # dict[str, PriceUpdate]

# Application shutdown
await source.stop()
```

---

## Environment Variables

| Variable | Default | Effect |
|----------|---------|--------|
| `MASSIVE_API_KEY` | _(unset)_ | If set and non-empty, uses real Polygon.io data; otherwise uses GBM simulator |

---

## Test Suite

**94 tests, all passing.** 7 test modules in `backend/tests/market/`.

| Module | Tests | Notes |
|--------|-------|-------|
| `test_models.py` | 11 | `PriceUpdate` dataclass — 100% coverage |
| `test_cache.py` | 17 | Including thread-safety stress test (8 threads × 500 updates) and zero-timestamp regression |
| `test_simulator.py` | 19 | Including full 10-ticker Cholesky stability (1000 steps) |
| `test_simulator_source.py` | 13 | Integration tests including case normalisation |
| `test_factory.py` | 7 | Lazy import and env var selection |
| `test_massive.py` | 13 | API mocking — 56% coverage (expected for an HTTP client) |
| `test_stream.py` | 14 | SSE generator: retry directive, JSON payload, version dedup, keepalive, disconnect, router factory |

Run:
```bash
cd backend
uv run --extra dev pytest -v              # All tests
uv run --extra dev pytest --cov=app       # With coverage
uv run --extra dev ruff check app/ tests/ # Lint
```

---

## Demo

```bash
cd backend
uv run market_data_demo.py
```

Live terminal dashboard with all 10 tickers, sparkline price charts, colour-coded direction, and a session summary on exit. Runs 60 seconds or until Ctrl+C.

---

## Code Review Fixes Applied

Seven issues were identified in review and resolved:

| # | Severity | Fix |
|---|----------|-----|
| 1 | High | `timestamp or time.time()` → `timestamp if timestamp is not None else time.time()` — zero timestamps were discarded |
| 2 | High | `cache.remove()` now increments `version` — watchlist removals now visible to SSE |
| 3 | Medium | SSE keepalive comments added — prevents proxy idle-timeout drops |
| 4 | Medium | `SimulatorDataSource.add_ticker/remove_ticker` now normalise case (`upper().strip()`) — matches `MassiveDataSource` |
| 5 | Medium | `stream.py` creates a fresh `APIRouter` per call — no duplicate route registration |
| 6 | Low | `factory.py` uses lazy imports — `massive` package only loaded when `MASSIVE_API_KEY` is set |
| 7 | Low | Dead duplicate guard removed from `_add_ticker_internal` |
