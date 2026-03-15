# Market Data Backend — Detailed Design

Implementation-ready design for the FinAlly market data subsystem. Covers the unified interface, in-memory price cache, GBM simulator, Massive API client, SSE streaming endpoint, and FastAPI lifecycle integration.

Everything in this document lives under `backend/app/market/`.

---

## Table of Contents

1. [File Structure](#1-file-structure)
2. [Data Model — `models.py`](#2-data-model)
3. [Price Cache — `cache.py`](#3-price-cache)
4. [Abstract Interface — `interface.py`](#4-abstract-interface)
5. [Seed Prices & Ticker Parameters — `seed_prices.py`](#5-seed-prices--ticker-parameters)
6. [GBM Simulator — `simulator.py`](#6-gbm-simulator)
7. [Massive API Client — `massive_client.py`](#7-massive-api-client)
8. [Factory — `factory.py`](#8-factory)
9. [SSE Streaming Endpoint — `stream.py`](#9-sse-streaming-endpoint)
10. [FastAPI Lifecycle Integration](#10-fastapi-lifecycle-integration)
11. [Watchlist Coordination](#11-watchlist-coordination)
12. [Testing Strategy](#12-testing-strategy)
13. [Error Handling & Edge Cases](#13-error-handling--edge-cases)
14. [Configuration Summary](#14-configuration-summary)

---

## 1. File Structure

```
backend/
  app/
    market/
      __init__.py             # Re-exports: PriceUpdate, PriceCache, MarketDataSource, create_market_data_source
      models.py               # PriceUpdate dataclass
      cache.py                # PriceCache (thread-safe in-memory store)
      interface.py            # MarketDataSource ABC
      seed_prices.py          # SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS, CORRELATION_GROUPS
      simulator.py            # GBMSimulator + SimulatorDataSource
      massive_client.py       # MassiveDataSource (Polygon.io REST polling)
      factory.py              # create_market_data_source()
      stream.py               # FastAPI SSE endpoint
```

### What each file is responsible for

| File | Responsibility |
|------|----------------|
| `models.py` | The `PriceUpdate` dataclass — the only data structure that leaves the market layer |
| `cache.py` | Thread-safe, version-stamped price store; producers write, consumers read |
| `interface.py` | `MarketDataSource` ABC — both implementations conform to this contract |
| `seed_prices.py` | Constants: starting prices, per-ticker GBM params, correlation groups |
| `simulator.py` | `GBMSimulator` (math) + `SimulatorDataSource` (async task wrapper) |
| `massive_client.py` | `MassiveDataSource` — wraps Polygon.io REST API as a polling background task |
| `factory.py` | Reads `MASSIVE_API_KEY` env var and returns the right implementation |
| `stream.py` | FastAPI router with SSE endpoint; reads from `PriceCache` |

---

## 2. Data Model

`backend/app/market/models.py`

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """
    Immutable snapshot of one price tick for one ticker.

    This is the only data type that crosses the boundary between the
    market data layer and the rest of the application. Everything
    downstream — the SSE stream, portfolio valuation, trade execution —
    works with PriceUpdate objects.
    """
    ticker: str
    price: float
    previous_price: float
    timestamp: float          # Unix epoch seconds
    change: float             # price - previous_price (signed)
    direction: str            # "up", "down", or "flat"
```

**Design notes:**
- `frozen=True` — immutable after creation; safe to pass around without defensive copies
- `slots=True` — ~30% less memory per instance; matters when the cache grows to thousands of ticks
- `direction` is pre-computed so downstream code never has to compare floats
- `change` is signed: positive = uptick, negative = downtick

---

## 3. Price Cache

`backend/app/market/cache.py`

The cache is the single point of truth for current prices. Producers (simulator or Massive poller) write to it; consumers (SSE endpoint, portfolio routes, trade execution) read from it.

```python
import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """
    Thread-safe in-memory store of the latest price per ticker.

    Uses a version counter so the SSE endpoint can detect changes
    without comparing dicts — it just checks whether version changed.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0

    # ------------------------------------------------------------------
    # Write API (called by data source background tasks)
    # ------------------------------------------------------------------

    def update(
        self,
        ticker: str,
        price: float,
        timestamp: float | None = None,
    ) -> PriceUpdate:
        """
        Write a new price for a ticker. Returns the resulting PriceUpdate.

        Thread-safe: acquires the lock before touching internal state.
        """
        with self._lock:
            ts = timestamp if timestamp is not None else time.time()
            previous = self._prices.get(ticker)
            previous_price = previous.price if previous else price

            if price > previous_price:
                direction = "up"
            elif price < previous_price:
                direction = "down"
            else:
                direction = "flat"

            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                previous_price=previous_price,
                timestamp=ts,
                change=round(price - previous_price, 4),
                direction=direction,
            )
            self._prices[ticker] = update
            self._version += 1
            return update

    def remove(self, ticker: str) -> None:
        """Remove a ticker from the cache (e.g., when removed from watchlist)."""
        with self._lock:
            self._prices.pop(ticker, None)
            self._version += 1

    # ------------------------------------------------------------------
    # Read API (called by SSE endpoint, portfolio routes, trade handler)
    # ------------------------------------------------------------------

    def get(self, ticker: str) -> PriceUpdate | None:
        """Return the latest PriceUpdate for a ticker, or None."""
        with self._lock:
            return self._prices.get(ticker)

    def get_price(self, ticker: str) -> float | None:
        """Convenience method: return just the price float, or None."""
        update = self.get(ticker)
        return update.price if update else None

    def get_all(self) -> dict[str, PriceUpdate]:
        """Return a snapshot of all current prices (shallow copy)."""
        with self._lock:
            return dict(self._prices)

    @property
    def version(self) -> int:
        """
        Monotonically increasing counter. Increments on every write.

        The SSE endpoint polls this to detect changes without full dict comparison.
        On CPython the int read is atomic under the GIL; on free-threaded builds
        (Python 3.13t+) this property should be moved under the lock.
        """
        return self._version
```

### How the SSE endpoint uses version

```python
# Pseudo-code showing version-based change detection
last_version = -1
while True:
    current_version = cache.version
    if current_version != last_version:
        payload = cache.get_all()
        yield f"data: {json.dumps(payload)}\n\n"
        last_version = current_version
    await asyncio.sleep(0.1)  # poll 10x/sec, push only on change
```

---

## 4. Abstract Interface

`backend/app/market/interface.py`

Both implementations must conform to this interface. Downstream code always types against `MarketDataSource`, never a concrete class.

```python
from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """
    Abstract interface for market data providers.

    Implementations: SimulatorDataSource, MassiveDataSource.
    Both write price updates into a shared PriceCache; they do NOT
    return prices directly.

    All methods are async to support both asyncio task management
    and potential async I/O in implementations.
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """
        Begin producing price updates for the given tickers.

        Starts the background task (simulator loop or API polling loop).
        Seeds the price cache with initial values before returning so
        the first SSE push has data immediately.
        """

    @abstractmethod
    async def stop(self) -> None:
        """
        Stop producing price updates and clean up resources.

        Cancels the background asyncio task. Safe to call if already stopped.
        """

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """
        Add a ticker to the active set.

        Idempotent — calling with an already-tracked ticker is a no-op.
        The new ticker will appear in the next price update cycle.
        """

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """
        Remove a ticker from the active set.

        Also removes the ticker from the PriceCache so stale data
        is not served to clients.
        """

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of active tickers (snapshot)."""
```

---

## 5. Seed Prices & Ticker Parameters

`backend/app/market/seed_prices.py`

Constants only — no logic. Imported by `simulator.py`.

```python
# Starting prices close to real-world values (as of early 2026).
# The simulator evolves from these via GBM; they drift over time.
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.0,
    "GOOGL": 175.0,
    "MSFT": 420.0,
    "AMZN": 185.0,
    "TSLA": 250.0,
    "NVDA": 800.0,
    "META": 500.0,
    "JPM": 195.0,
    "V": 280.0,
    "NFLX": 600.0,
}

# Per-ticker GBM parameters.
# mu   = annualized drift (expected return)
# sigma = annualized volatility
# These reflect real-world characteristics: TSLA/NVDA are high-vol,
# JPM/V are defensive low-vol names.
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},   # High vol, lower drift
    "NVDA":  {"sigma": 0.40, "mu": 0.08},   # High vol, strong growth drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},   # Stable bank stock
    "V":     {"sigma": 0.17, "mu": 0.04},   # Stable payments stock
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

# Fallback for dynamically added tickers not in the list above.
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Correlation groups for Cholesky decomposition.
# Tickers in the same group move together more than cross-group.
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech":    {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
    # TSLA is intentionally ungrouped — it correlates weakly with everything
}

# Pairwise correlations
TECH_CORR: float = 0.6       # within tech group
FINANCE_CORR: float = 0.5    # within finance group
CROSS_GROUP_CORR: float = 0.3  # cross-sector, TSLA, and unknowns
```

---

## 6. GBM Simulator

`backend/app/market/simulator.py`

Two classes:
- `GBMSimulator` — pure math; generates correlated price steps
- `SimulatorDataSource` — wraps `GBMSimulator` in an async loop; implements `MarketDataSource`

### 6.1 GBM Math

At each time step, a stock price evolves as:

```
S(t + dt) = S(t) * exp((mu - sigma²/2) * dt + sigma * sqrt(dt) * Z)
```

Where:
- `mu` = annualized drift
- `sigma` = annualized volatility
- `dt` = time step as a fraction of a trading year
- `Z` = standard normal random variable

For 500ms updates with 252 trading days × 6.5 hours × 3600 seconds:
```
dt = 0.5 / (252 * 6.5 * 3600) ≈ 8.5e-8
```

This tiny `dt` produces sub-cent moves per tick that accumulate naturally.

### 6.2 Correlated Moves via Cholesky Decomposition

Real stocks in the same sector co-move. We model this by:
1. Building an `n×n` correlation matrix `C` from the pairwise correlations
2. Computing the Cholesky factor `L = cholesky(C)`
3. Each step: `z_correlated = L @ z_independent` where `z_independent ~ N(0, I)`

This guarantees the correlated draws have the desired covariance structure.

### 6.3 Full Implementation

```python
import asyncio
import logging
import math
import random

import numpy as np

from .cache import PriceCache
from .interface import MarketDataSource
from .models import PriceUpdate
from .seed_prices import (
    CORRELATION_GROUPS,
    CROSS_GROUP_CORR,
    DEFAULT_PARAMS,
    FINANCE_CORR,
    SEED_PRICES,
    TECH_CORR,
    TICKER_PARAMS,
)

logger = logging.getLogger(__name__)

# dt for 500ms updates on a ~252-trading-day, 6.5-hour trading day calendar
_DT: float = 0.5 / (252 * 6.5 * 3600)


class GBMSimulator:
    """
    Generates correlated GBM price paths for multiple tickers.

    Stateful: maintains current prices and the Cholesky decomposition.
    Tickers can be added/removed dynamically; the Cholesky matrix is
    rebuilt on each change (O(n²), fine for n < 100).
    """

    def __init__(
        self,
        tickers: list[str],
        dt: float = _DT,
        event_probability: float = 0.001,
    ) -> None:
        self._dt = dt
        self._event_prob = event_probability
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}
        self._tickers: list[str] = []
        self._cholesky: np.ndarray | None = None

        for ticker in tickers:
            self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_ticker(self, ticker: str) -> None:
        """Add a ticker. No-op if already tracked."""
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker. No-op if not tracked."""
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def get_tickers(self) -> list[str]:
        """Return the list of currently tracked tickers."""
        return list(self._tickers)

    def step(self) -> dict[str, float]:
        """
        Advance one time step.

        Returns a dict {ticker: new_price} for all tracked tickers.
        Prices are rounded to 2 decimal places.
        """
        n = len(self._tickers)
        if n == 0:
            return {}

        # Correlated normal draws
        z_independent = np.random.standard_normal(n)
        z = self._cholesky @ z_independent if self._cholesky is not None else z_independent

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            params = self._params[ticker]
            mu = params["mu"]
            sigma = params["sigma"]

            # Standard GBM step (log-normal ensures price > 0)
            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * float(z[i])
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random shock event (~0.1% chance per tick)
            if random.random() < self._event_prob:
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= (1 + shock)
                logger.debug("Shock event on %s: %.1f%%", ticker, shock * 100)

            result[ticker] = round(self._prices[ticker], 2)

        return result

    def get_price(self, ticker: str) -> float | None:
        """Return the current price for a ticker, or None."""
        return self._prices.get(ticker)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _add_ticker_internal(self, ticker: str) -> None:
        """Add ticker to internal state without rebuilding Cholesky."""
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = TICKER_PARAMS.get(ticker, DEFAULT_PARAMS)

    def _rebuild_cholesky(self) -> None:
        """Rebuild the Cholesky factor of the correlation matrix."""
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return

        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = self._pairwise_correlation(self._tickers[i], self._tickers[j])
                corr[i, j] = rho
                corr[j, i] = rho

        try:
            self._cholesky = np.linalg.cholesky(corr)
        except np.linalg.LinAlgError:
            # Correlation matrix is not positive definite — shouldn't happen with
            # our parameters, but fall back to independent draws if it does.
            logger.warning("Cholesky decomposition failed; using independent draws")
            self._cholesky = None

    @staticmethod
    def _pairwise_correlation(t1: str, t2: str) -> float:
        """Return the target pairwise correlation between two tickers."""
        tech = CORRELATION_GROUPS["tech"]
        finance = CORRELATION_GROUPS["finance"]

        if t1 in tech and t2 in tech:
            return TECH_CORR
        if t1 in finance and t2 in finance:
            return FINANCE_CORR
        # TSLA is a loner; unknown tickers use cross-group default
        return CROSS_GROUP_CORR


class SimulatorDataSource(MarketDataSource):
    """
    MarketDataSource implementation backed by GBMSimulator.

    Runs a background asyncio task that calls simulator.step() every
    `update_interval` seconds and writes results to the PriceCache.
    """

    def __init__(
        self,
        price_cache: PriceCache,
        update_interval: float = 0.5,
    ) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        """
        Initialize the simulator with starting tickers and begin the update loop.

        Seeds the cache immediately so the first SSE response has prices.
        """
        self._sim = GBMSimulator(tickers=tickers)

        # Seed cache with initial prices before the first tick
        initial = self._sim.step()
        for ticker, price in initial.items():
            self._cache.update(ticker=ticker, price=price)

        self._task = asyncio.create_task(self._run_loop(), name="gbm-simulator")
        logger.info("SimulatorDataSource started with %d tickers", len(tickers))

    async def stop(self) -> None:
        """Cancel the background task. Safe to call if already stopped."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("SimulatorDataSource stopped")

    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the live simulation."""
        if self._sim is None:
            return
        self._sim.add_ticker(ticker)
        # Seed the cache immediately with the starting price
        price = self._sim.get_price(ticker)
        if price is not None:
            self._cache.update(ticker=ticker, price=price)

    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from simulation and cache."""
        if self._sim is not None:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        """Return tickers currently tracked by the simulator."""
        return self._sim.get_tickers() if self._sim else []

    async def _run_loop(self) -> None:
        """Main loop: step the simulator and update the cache every interval."""
        while True:
            try:
                prices = self._sim.step()
                for ticker, price in prices.items():
                    self._cache.update(ticker=ticker, price=price)
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unexpected error in simulator loop; continuing")
                await asyncio.sleep(self._interval)
```

---

## 7. Massive API Client

`backend/app/market/massive_client.py`

Polls Polygon.io (rebranded as "Massive") via their `massive` Python package. One API call per poll cycle fetches all tickers simultaneously.

### 7.1 Package & Auth

```bash
uv add massive
```

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

# Reads MASSIVE_API_KEY from environment automatically
client = RESTClient()

# Or explicit:
client = RESTClient(api_key="your_key_here")
```

### 7.2 Primary Endpoint: Snapshot All

```python
# One API call fetches all tickers — critical for free tier (5 req/min)
snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA"],
)

for snap in snapshots:
    print(f"{snap.ticker}: ${snap.last_trade.price}")
    print(f"  Day change: {snap.day.change_percent:.2f}%")
    print(f"  Bid/Ask: {snap.last_quote.bid_price} / {snap.last_quote.ask_price}")
```

Snapshot response fields used by FinAlly:

| Field | Used for |
|-------|----------|
| `snap.ticker` | Ticker key |
| `snap.last_trade.price` | Current price |
| `snap.last_trade.timestamp` | Timestamp (Unix ms — divide by 1000) |
| `snap.day.previous_close` | Previous price for `change` / `direction` |

### 7.3 Full Implementation

```python
import asyncio
import logging
from typing import Any

from massive import RESTClient
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


class MassiveDataSource(MarketDataSource):
    """
    MarketDataSource implementation that polls the Polygon.io REST API.

    Poll interval should be:
      - Free tier (5 req/min):  15 seconds minimum
      - Paid tier:              2-5 seconds

    The synchronous Massive client is run in a thread pool via
    asyncio.to_thread() to avoid blocking the event loop.
    """

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
    ) -> None:
        self._client = RESTClient(api_key=api_key)
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        """
        Seed the cache with an immediate poll, then start the polling loop.
        """
        self._tickers = list(tickers)
        # Seed immediately so SSE has data on first connect
        await self._poll_once()
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info(
            "MassiveDataSource started: %d tickers, interval=%.1fs",
            len(tickers),
            self._interval,
        )

    async def stop(self) -> None:
        """Cancel the polling task."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("MassiveDataSource stopped")

    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker; it will be included in the next poll."""
        if ticker not in self._tickers:
            self._tickers.append(ticker)

    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from polling and from the cache."""
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # ------------------------------------------------------------------
    # Internal polling logic
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Repeatedly poll at the configured interval."""
        while True:
            try:
                await asyncio.sleep(self._interval)
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in Massive poll loop; retrying after interval")

    async def _poll_once(self) -> None:
        """Fetch snapshots for all tracked tickers and update the cache."""
        if not self._tickers:
            return
        tickers_snapshot = list(self._tickers)  # copy to avoid mutation during call
        try:
            snapshots = await asyncio.to_thread(self._fetch_snapshots, tickers_snapshot)
            for snap in snapshots:
                try:
                    price = snap.last_trade.price
                    # Timestamp from API is Unix milliseconds
                    ts = snap.last_trade.timestamp / 1000.0
                    self._cache.update(ticker=snap.ticker, price=price, timestamp=ts)
                except (AttributeError, TypeError, ZeroDivisionError):
                    logger.warning("Malformed snapshot for %s; skipping", getattr(snap, "ticker", "?"))
        except Exception:
            logger.exception("Failed to fetch snapshots from Massive API")

    def _fetch_snapshots(self, tickers: list[str]) -> list[Any]:
        """
        Synchronous call to Massive REST API. Runs in a thread pool.

        Fetches all tickers in a single API call — critical for staying
        within the free tier rate limit of 5 requests/minute.
        """
        return list(
            self._client.get_snapshot_all(
                market_type=SnapshotMarketType.STOCKS,
                tickers=tickers,
            )
        )
```

### 7.4 Rate Limit Strategy

| Tier | Limit | Poll Interval |
|------|-------|---------------|
| Free | 5 req/min | 15 seconds (default) |
| Starter | Unlimited | 5 seconds |
| Developer+ | Unlimited | 2 seconds |

The factory function sets the interval from an environment variable so it can be tuned without code changes:

```python
poll_interval = float(os.environ.get("MASSIVE_POLL_INTERVAL", "15.0"))
```

---

## 8. Factory

`backend/app/market/factory.py`

Reads the environment and returns the appropriate `MarketDataSource`.

```python
import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """
    Create the appropriate market data source based on environment.

    If MASSIVE_API_KEY is set and non-empty: returns MassiveDataSource.
    Otherwise: returns SimulatorDataSource.

    Both implementations conform to MarketDataSource. Downstream code
    should never import concrete classes directly — always use this factory.
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        from .massive_client import MassiveDataSource
        poll_interval = float(os.environ.get("MASSIVE_POLL_INTERVAL", "15.0"))
        logger.info("Using Massive (Polygon.io) data source, interval=%.1fs", poll_interval)
        return MassiveDataSource(api_key=api_key, price_cache=price_cache, poll_interval=poll_interval)
    else:
        from .simulator import SimulatorDataSource
        logger.info("Using GBM simulator (no MASSIVE_API_KEY set)")
        return SimulatorDataSource(price_cache=price_cache)
```

**Why lazy imports inside the factory?**
The `massive` package is a hard dependency (`pyproject.toml` declares it) but this keeps the import structure explicit and makes it trivial to test the simulator path without the Massive package installed.

---

## 9. SSE Streaming Endpoint

`backend/app/market/stream.py`

Serves `GET /api/stream/prices` as a long-lived Server-Sent Events connection. The client uses the native browser `EventSource` API which handles reconnection automatically.

```python
import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from .cache import PriceCache

logger = logging.getLogger(__name__)


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """
    Factory that returns a FastAPI router with the SSE endpoint.

    Takes the shared PriceCache as a dependency rather than using a
    module-level global, which makes the router testable in isolation.
    """
    router = APIRouter()

    @router.get("/api/stream/prices")
    async def stream_prices() -> StreamingResponse:
        """
        Long-lived SSE stream of price updates.

        Clients connect once and receive continuous updates. The connection
        stays open until the client disconnects or the server shuts down.
        EventSource auto-reconnects after the `retry` interval.
        """
        return StreamingResponse(
            _generate_events(price_cache),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
                "Connection": "keep-alive",
            },
        )

    return router


async def _generate_events(cache: PriceCache) -> AsyncGenerator[str, None]:
    """
    Async generator that yields SSE-formatted price events.

    Uses version-based change detection: only sends a new event when the
    cache has been updated (version counter changed). This avoids sending
    redundant payloads on every poll cycle.

    Yields a keepalive comment every 15 seconds if no price changes occur,
    to prevent proxies from closing the connection due to inactivity.
    """
    # Tell the browser to reconnect after 1 second if the connection drops
    yield "retry: 1000\n\n"

    last_version = -1
    last_keepalive = asyncio.get_event_loop().time()

    while True:
        try:
            current_version = cache.version
            now = asyncio.get_event_loop().time()

            if current_version != last_version:
                prices = cache.get_all()
                payload = {
                    ticker: {
                        "ticker": update.ticker,
                        "price": update.price,
                        "previous_price": update.previous_price,
                        "change": update.change,
                        "direction": update.direction,
                        "timestamp": update.timestamp,
                    }
                    for ticker, update in prices.items()
                }
                yield f"data: {json.dumps(payload)}\n\n"
                last_version = current_version
                last_keepalive = now
            elif now - last_keepalive > 15.0:
                # SSE comment as keepalive (invisible to EventSource handler)
                yield ": keepalive\n\n"
                last_keepalive = now

            await asyncio.sleep(0.1)  # poll cache at 10Hz, push only on change

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error in SSE generator; continuing")
            await asyncio.sleep(1.0)
```

### SSE Event Format

Each event is a JSON object keyed by ticker:

```
data: {"AAPL": {"ticker": "AAPL", "price": 191.23, "previous_price": 191.10, "change": 0.13, "direction": "up", "timestamp": 1710000000.123}, "GOOGL": {...}, ...}

```

The frontend receives this in the `EventSource` `onmessage` handler:

```typescript
const source = new EventSource('/api/stream/prices');
source.onmessage = (event) => {
  const prices: Record<string, PriceUpdate> = JSON.parse(event.data);
  // prices["AAPL"].price, prices["AAPL"].direction, etc.
};
source.onerror = () => {
  // EventSource auto-reconnects; update connection status indicator
};
```

---

## 10. FastAPI Lifecycle Integration

`backend/app/main.py` (relevant sections)

The market data system is initialized on app startup and torn down cleanly on shutdown using FastAPI's lifespan context manager (preferred over deprecated `@app.on_event`).

```python
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.market import PriceCache, create_market_data_source
from app.market.stream import create_stream_router
from app.db import get_default_watchlist, init_db

logger = logging.getLogger(__name__)

# Module-level singletons shared across routes
price_cache: PriceCache | None = None
market_source = None  # MarketDataSource


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan: startup → yield → shutdown."""
    global price_cache, market_source

    # 1. Initialize the database (create schema, seed defaults if needed)
    await init_db()

    # 2. Create the shared price cache
    price_cache = PriceCache()

    # 3. Create the data source (simulator or Massive based on env)
    market_source = create_market_data_source(price_cache)

    # 4. Load the watchlist from DB and start the data source
    initial_tickers = await get_default_watchlist()
    await market_source.start(initial_tickers)
    logger.info("Market data started for tickers: %s", initial_tickers)

    yield  # Application runs here

    # Shutdown: stop the background task cleanly
    if market_source:
        await market_source.stop()
    logger.info("Market data stopped")


app = FastAPI(lifespan=lifespan, title="FinAlly API")

# Mount SSE stream router
app.include_router(create_stream_router(price_cache))

# Mount other API routers
# app.include_router(portfolio_router)
# app.include_router(watchlist_router)
# app.include_router(chat_router)

# Serve Next.js static export (must be last — catchall)
app.mount("/", StaticFiles(directory="static", html=True), name="static")
```

### Making the cache available to route handlers

Use FastAPI's dependency injection rather than importing module-level globals:

```python
from fastapi import Depends
from app.market import PriceCache

def get_price_cache() -> PriceCache:
    """Dependency that returns the shared PriceCache."""
    # In a real app, store on app.state during lifespan
    return price_cache

@app.get("/api/portfolio")
async def get_portfolio(cache: PriceCache = Depends(get_price_cache)):
    aapl_price = cache.get_price("AAPL")
    ...
```

Or attach to `app.state` during lifespan for cleaner dependency injection:

```python
# In lifespan:
app.state.price_cache = price_cache
app.state.market_source = market_source

# Dependency:
def get_price_cache(request: Request) -> PriceCache:
    return request.app.state.price_cache
```

---

## 11. Watchlist Coordination

When the user adds or removes a ticker from the watchlist (via the watchlist API endpoints), the market data source must be updated synchronously so prices start/stop flowing immediately.

```python
# In the watchlist router:
from fastapi import APIRouter, HTTPException, Depends
from app.market import MarketDataSource, PriceCache

router = APIRouter(prefix="/api/watchlist")


@router.post("")
async def add_to_watchlist(
    body: AddTickerRequest,
    source: MarketDataSource = Depends(get_market_source),
    db = Depends(get_db),
):
    ticker = body.ticker.upper().strip()

    # Validate ticker (optional: check against known list or let Massive handle it)
    # ...

    # Persist to DB
    await db.add_watchlist_ticker(ticker)

    # Start tracking in market data source
    await source.add_ticker(ticker)

    return {"ticker": ticker, "status": "added"}


@router.delete("/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
    db = Depends(get_db),
):
    ticker = ticker.upper()

    # Remove from DB
    await db.remove_watchlist_ticker(ticker)

    # Stop tracking — also removes from PriceCache
    await source.remove_ticker(ticker)

    return {"ticker": ticker, "status": "removed"}
```

### Sequence diagram

```
User clicks "Add PYPL"
     │
     ▼
POST /api/watchlist {ticker: "PYPL"}
     │
     ├─► DB: INSERT INTO watchlist (ticker="PYPL")
     │
     └─► market_source.add_ticker("PYPL")
              │
              ├─► GBMSimulator.add_ticker("PYPL")
              │     ├── seed price = random($50-$300) if not in SEED_PRICES
              │     └── rebuild Cholesky matrix
              │
              └─► cache.update("PYPL", seed_price)
                      │
                      └─► SSE stream pushes PYPL on next tick
```

---

## 12. Testing Strategy

### 12.1 Unit Tests — Models

`backend/tests/market/test_models.py`

```python
from app.market.models import PriceUpdate


def test_price_update_is_frozen():
    u = PriceUpdate("AAPL", 190.0, 189.0, 1234567890.0, 1.0, "up")
    try:
        u.price = 200.0
        assert False, "Should have raised FrozenInstanceError"
    except Exception:
        pass  # Expected


def test_direction_values():
    up = PriceUpdate("X", 100.0, 99.0, 0.0, 1.0, "up")
    down = PriceUpdate("X", 99.0, 100.0, 0.0, -1.0, "down")
    flat = PriceUpdate("X", 100.0, 100.0, 0.0, 0.0, "flat")
    assert up.direction == "up"
    assert down.direction == "down"
    assert flat.direction == "flat"
```

### 12.2 Unit Tests — PriceCache

`backend/tests/market/test_cache.py`

```python
import time
from app.market.cache import PriceCache


def test_update_returns_price_update():
    cache = PriceCache()
    u = cache.update("AAPL", 190.0)
    assert u.ticker == "AAPL"
    assert u.price == 190.0
    assert u.direction == "flat"  # First update: previous == price


def test_direction_computed_correctly():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    u = cache.update("AAPL", 191.0)
    assert u.direction == "up"
    assert u.change == pytest.approx(1.0, abs=0.01)

    u2 = cache.update("AAPL", 190.5)
    assert u2.direction == "down"


def test_version_increments_on_write():
    cache = PriceCache()
    v0 = cache.version
    cache.update("AAPL", 190.0)
    assert cache.version == v0 + 1
    cache.update("GOOGL", 175.0)
    assert cache.version == v0 + 2


def test_remove_clears_ticker():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    cache.remove("AAPL")
    assert cache.get("AAPL") is None


def test_get_all_returns_copy():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    snapshot = cache.get_all()
    cache.update("AAPL", 191.0)
    # snapshot should not reflect the new update
    assert snapshot["AAPL"].price == 190.0


def test_thread_safety():
    """Multiple threads writing concurrently should not corrupt state."""
    import threading
    cache = PriceCache()
    errors = []

    def writer(ticker: str):
        for i in range(100):
            try:
                cache.update(ticker, float(100 + i))
            except Exception as e:
                errors.append(e)

    threads = [threading.Thread(target=writer, args=(f"T{i}",)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(cache.get_all()) == 10
```

### 12.3 Unit Tests — GBM Simulator

`backend/tests/market/test_simulator.py`

```python
import pytest
from app.market.simulator import GBMSimulator


def test_step_returns_all_tickers():
    sim = GBMSimulator(["AAPL", "GOOGL"])
    result = sim.step()
    assert set(result.keys()) == {"AAPL", "GOOGL"}


def test_prices_are_positive():
    sim = GBMSimulator(["AAPL", "TSLA", "NVDA"])
    for _ in range(100):
        prices = sim.step()
        for ticker, price in prices.items():
            assert price > 0, f"{ticker} went negative: {price}"


def test_add_ticker_adds_to_step():
    sim = GBMSimulator(["AAPL"])
    sim.add_ticker("GOOGL")
    result = sim.step()
    assert "GOOGL" in result


def test_remove_ticker_removes_from_step():
    sim = GBMSimulator(["AAPL", "GOOGL"])
    sim.remove_ticker("GOOGL")
    result = sim.step()
    assert "GOOGL" not in result


def test_seed_prices_used_for_known_tickers():
    from app.market.seed_prices import SEED_PRICES
    sim = GBMSimulator(["AAPL"])
    # After one step, price should still be close to seed
    price = sim.get_price("AAPL")
    assert abs(price - SEED_PRICES["AAPL"]) < SEED_PRICES["AAPL"] * 0.1


def test_cholesky_rebuilt_on_add():
    """Adding a ticker should not cause the next step to crash."""
    sim = GBMSimulator(["AAPL", "GOOGL", "MSFT"])
    sim.add_ticker("TSLA")
    sim.step()  # Should not raise


def test_full_default_watchlist():
    """Cholesky succeeds for all 10 default tickers."""
    tickers = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]
    sim = GBMSimulator(tickers)
    result = sim.step()
    assert len(result) == 10
    assert all(p > 0 for p in result.values())
```

### 12.4 Unit Tests — SimulatorDataSource

`backend/tests/market/test_simulator_source.py`

```python
import asyncio
import pytest
from app.market.cache import PriceCache
from app.market.simulator import SimulatorDataSource


@pytest.mark.asyncio
async def test_start_seeds_cache():
    cache = PriceCache()
    source = SimulatorDataSource(cache)
    await source.start(["AAPL", "GOOGL"])
    assert cache.get("AAPL") is not None
    assert cache.get("GOOGL") is not None
    await source.stop()


@pytest.mark.asyncio
async def test_prices_update_over_time():
    cache = PriceCache()
    source = SimulatorDataSource(cache, update_interval=0.05)
    await source.start(["AAPL"])
    v0 = cache.version
    await asyncio.sleep(0.2)
    assert cache.version > v0
    await source.stop()


@pytest.mark.asyncio
async def test_add_remove_ticker():
    cache = PriceCache()
    source = SimulatorDataSource(cache)
    await source.start(["AAPL"])
    await source.add_ticker("TSLA")
    assert "TSLA" in source.get_tickers()
    await source.remove_ticker("TSLA")
    assert "TSLA" not in source.get_tickers()
    assert cache.get("TSLA") is None
    await source.stop()


@pytest.mark.asyncio
async def test_stop_is_idempotent():
    cache = PriceCache()
    source = SimulatorDataSource(cache)
    await source.start(["AAPL"])
    await source.stop()
    await source.stop()  # Should not raise
```

### 12.5 Unit Tests — Factory

`backend/tests/market/test_factory.py`

```python
import os
import pytest
from app.market.cache import PriceCache
from app.market.factory import create_market_data_source
from app.market.simulator import SimulatorDataSource


def test_no_api_key_returns_simulator(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    cache = PriceCache()
    source = create_market_data_source(cache)
    assert isinstance(source, SimulatorDataSource)


def test_empty_api_key_returns_simulator(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "")
    cache = PriceCache()
    source = create_market_data_source(cache)
    assert isinstance(source, SimulatorDataSource)


def test_api_key_set_returns_massive(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test_key_12345")
    cache = PriceCache()
    source = create_market_data_source(cache)
    from app.market.massive_client import MassiveDataSource
    assert isinstance(source, MassiveDataSource)
```

### 12.6 Unit Tests — Massive Client

`backend/tests/market/test_massive.py`

```python
import asyncio
from unittest.mock import MagicMock, patch
import pytest
from app.market.cache import PriceCache
from app.market.massive_client import MassiveDataSource


def make_snapshot(ticker: str, price: float, ts_ms: int = 1_700_000_000_000):
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade.price = price
    snap.last_trade.timestamp = ts_ms
    return snap


@pytest.mark.asyncio
async def test_poll_updates_cache():
    cache = PriceCache()
    source = MassiveDataSource(api_key="test", price_cache=cache)
    snapshots = [make_snapshot("AAPL", 190.0), make_snapshot("GOOGL", 175.0)]

    with patch.object(source, "_fetch_snapshots", return_value=snapshots):
        await source._poll_once()

    assert cache.get_price("AAPL") == 190.0
    assert cache.get_price("GOOGL") == 175.0


@pytest.mark.asyncio
async def test_malformed_snapshot_is_skipped():
    cache = PriceCache()
    source = MassiveDataSource(api_key="test", price_cache=cache)
    bad = MagicMock()
    bad.ticker = "BAD"
    bad.last_trade.price = None  # Will cause TypeError when written to cache
    good = make_snapshot("AAPL", 190.0)

    with patch.object(source, "_fetch_snapshots", return_value=[bad, good]):
        await source._poll_once()  # Should not raise

    assert cache.get_price("AAPL") == 190.0


@pytest.mark.asyncio
async def test_timestamp_converted_from_ms():
    cache = PriceCache()
    source = MassiveDataSource(api_key="test", price_cache=cache)
    ts_ms = 1_700_000_000_000
    snap = make_snapshot("AAPL", 190.0, ts_ms=ts_ms)

    with patch.object(source, "_fetch_snapshots", return_value=[snap]):
        await source._poll_once()

    update = cache.get("AAPL")
    assert abs(update.timestamp - ts_ms / 1000.0) < 0.001


@pytest.mark.asyncio
async def test_remove_ticker_removes_from_cache():
    cache = PriceCache()
    source = MassiveDataSource(api_key="test", price_cache=cache)
    source._tickers = ["AAPL", "GOOGL"]
    cache.update("AAPL", 190.0)
    await source.remove_ticker("AAPL")
    assert "AAPL" not in source.get_tickers()
    assert cache.get("AAPL") is None
```

### 12.7 Integration Test — SSE Endpoint

`backend/tests/market/test_stream.py`

```python
import json
import pytest
from httpx import AsyncClient, ASGITransport
from fastapi import FastAPI

from app.market.cache import PriceCache
from app.market.stream import create_stream_router


@pytest.mark.asyncio
async def test_sse_returns_price_data():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    cache.update("GOOGL", 175.0)

    router = create_stream_router(cache)
    app = FastAPI()
    app.include_router(router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream("GET", "/api/stream/prices") as response:
            assert response.status_code == 200
            assert "text/event-stream" in response.headers["content-type"]

            # Read the first two lines (retry directive + first data event)
            lines = []
            async for line in response.aiter_lines():
                lines.append(line)
                if len(lines) >= 2:
                    break

    retry_line = lines[0]
    data_line = lines[1]
    assert retry_line == "retry: 1000"
    assert data_line.startswith("data: ")
    payload = json.loads(data_line[6:])
    assert "AAPL" in payload
    assert payload["AAPL"]["price"] == 190.0
```

---

## 13. Error Handling & Edge Cases

### Empty watchlist

Both data sources handle the case where `tickers=[]`:
- `GBMSimulator.step()` returns `{}` immediately
- `MassiveDataSource._poll_once()` returns early without making an API call
- The SSE endpoint yields `data: {}\n\n` which the frontend ignores (empty payload check)

### Unknown ticker (dynamically added)

`GBMSimulator` seeds unknown tickers with:
- A random price between $50 and $300
- Default GBM parameters (`sigma=0.25, mu=0.05`)

`MassiveDataSource` will include the ticker in the next API call. If Polygon.io doesn't recognize it, the snapshot will simply be absent from the response; the cache retains the last known value (or nothing if it was never seen).

### Massive API errors

- `429 Too Many Requests`: The poll loop catches the exception, logs a warning, and continues. The cache retains stale prices until the next successful poll.
- `401 Unauthorized`: Logs the error. Consider failing fast at startup to surface misconfiguration early.
- `5xx Server Error`: Massive client has built-in 3-retry logic. If all retries fail, exception bubbles up to `_poll_once` handler, which logs and continues.

### Simulator numeric edge cases

- GBM is multiplicative (`exp(...)`) so prices can never reach zero
- If `sigma` is set unrealistically high (e.g., 5.0), prices can drift wildly but remain positive
- The Cholesky decomposition can fail if the correlation matrix is not positive definite. This is caught and logged; simulation continues with independent draws

### SSE client disconnect

FastAPI/Starlette detects the disconnect and cancels the `_generate_events` generator via `asyncio.CancelledError`. The generator re-raises `CancelledError` properly and the connection cleans up. No special handling needed.

### Duplicate ticker in watchlist

Both sources are idempotent for `add_ticker`:
- `GBMSimulator.add_ticker()` checks `if ticker in self._prices: return`
- `MassiveDataSource.add_ticker()` checks `if ticker not in self._tickers`

---

## 14. Configuration Summary

All market data behavior is configured via environment variables read at startup:

| Variable | Default | Effect |
|----------|---------|--------|
| `MASSIVE_API_KEY` | (empty) | If set: use Massive/Polygon.io REST API; if empty: use simulator |
| `MASSIVE_POLL_INTERVAL` | `15.0` | Seconds between Massive API polls (free tier: 15+; paid: 2-5) |

### `pyproject.toml` dependencies

```toml
[project]
name = "finally-backend"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "numpy>=2.0",
    "massive>=1.0.0",
    "httpx>=0.27",      # for SSE integration tests
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
]

[tool.hatch.build.targets.wheel]
packages = ["app"]  # Required for uv sync to work correctly
```

### `__init__.py` re-exports

`backend/app/market/__init__.py`

```python
from .cache import PriceCache
from .factory import create_market_data_source
from .interface import MarketDataSource
from .models import PriceUpdate

__all__ = ["PriceCache", "MarketDataSource", "PriceUpdate", "create_market_data_source"]
```

This is the public API of the market data layer. All other modules (`simulator.py`, `massive_client.py`, `stream.py`, `seed_prices.py`) are internal implementation details.

---

## Quick-Start Usage

```python
from app.market import PriceCache, create_market_data_source

# App startup
cache = PriceCache()
source = create_market_data_source(cache)  # reads MASSIVE_API_KEY
await source.start(["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"])

# Read prices (from any route handler or background task)
update = cache.get("AAPL")            # PriceUpdate | None
price = cache.get_price("AAPL")       # float | None
all_prices = cache.get_all()          # dict[str, PriceUpdate]

# Dynamic watchlist management
await source.add_ticker("PYPL")
await source.remove_ticker("GOOGL")

# App shutdown
await source.stop()
```
