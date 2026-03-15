# Market Simulator — Approach and Code Structure

## Purpose

The simulator generates realistic-looking stock price movements without any external API. It is the **default data source** when `MASSIVE_API_KEY` is not set, making the app fully functional for development, demos, and users without a paid data subscription.

Goals:
- Prices move in ways that look like real stocks (continuous, bounded, mean-reverting over long runs)
- Related stocks move together (tech stocks correlate, finance stocks correlate)
- Occasional dramatic spikes for visual interest
- Runs entirely in-process — no threads, no processes, no external calls

---

## Mathematical Foundation: Geometric Brownian Motion (GBM)

GBM is the standard model for stock prices in quantitative finance (Black-Scholes uses it). It produces prices that:
- Are always positive (prices can't go negative)
- Have log-normally distributed returns
- Exhibit realistic variance scaling (volatility scales with `sqrt(time)`)

**The formula, applied at each time step:**

```
S(t+dt) = S(t) * exp((μ - σ²/2) * dt + σ * √dt * Z)
```

Where:
- `S(t)` — current price
- `μ` (mu) — annualized drift (expected annual return, e.g., 0.05 = 5%/year)
- `σ` (sigma) — annualized volatility (e.g., 0.25 = 25%/year)
- `dt` — time step as a fraction of a trading year
- `Z` — standard normal random variable (correlated across tickers via Cholesky)

**Why the `(μ - σ²/2)` term?**
This is the Itô correction. Without it, the *expected price* would drift upward too fast. The `σ²/2` term corrects for the convexity of the exponential, ensuring the expected price grows at rate `μ`.

### Time step size

```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # = 5,896,800 seconds
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ≈ 8.48e-8
```

With a 500ms update interval, `dt` is extremely small. This produces sub-cent moves per tick that accumulate naturally over hours/days of simulated time. The numbers look realistic: a stock with 25% annual volatility moves about 0.003% per 500ms tick on average, which is a few cents on a $100 stock.

---

## Correlated Moves

Real stocks in the same sector move together. When tech sells off, AAPL, GOOGL, and MSFT all drop. The simulator models this using **Cholesky decomposition** of a correlation matrix.

### Correlation structure

```python
CORRELATION_GROUPS = {
    "tech":    {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR    = 0.6   # Tech stocks: strong positive correlation
INTRA_FINANCE_CORR = 0.5   # Finance stocks: moderate correlation
CROSS_GROUP_CORR   = 0.3   # Between sectors / unknown tickers
TSLA_CORR          = 0.3   # TSLA does its own thing (even within tech)
```

### How Cholesky correlation works

Given `n` tickers, we build an `n×n` symmetric correlation matrix `Σ` where:
- `Σ[i,i] = 1` (each stock is perfectly correlated with itself)
- `Σ[i,j] = ρ` (sector-based correlation coefficient)

Then we compute the Cholesky factorization: `L = cholesky(Σ)` such that `L @ L.T = Σ`.

To generate `n` correlated draws per tick:
1. Generate `n` **independent** standard normals: `Z_indep ~ N(0, I)`
2. Multiply by the Cholesky factor: `Z_corr = L @ Z_indep`

`Z_corr` is now a vector of correlated normals with the desired correlation structure. Each `Z_corr[i]` is plugged into the GBM formula for ticker `i`.

**The Cholesky matrix is rebuilt** whenever tickers are added or removed. With `n < 50` tickers this is O(n²) and takes microseconds.

```python
def _rebuild_cholesky(self) -> None:
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

    self._cholesky = np.linalg.cholesky(corr)
```

---

## Random Shock Events

To add visual drama (sudden spikes/drops visible in sparklines), the simulator occasionally applies a large instantaneous shock:

```python
event_probability = 0.001  # 0.1% chance per tick per ticker

if random.random() < self._event_prob:
    shock_magnitude = random.uniform(0.02, 0.05)   # 2–5% move
    shock_sign = random.choice([-1, 1])
    self._prices[ticker] *= 1 + shock_magnitude * shock_sign
```

With 10 tickers at 2 ticks/second, an event occurs roughly every **50 seconds** on average. This creates visible spikes in the sparklines without being so frequent that prices look chaotic.

---

## Seed Prices and Parameters

Each default ticker starts from a realistic price and has tuned volatility/drift:

```python
SEED_PRICES = {
    "AAPL": 190.00,   "GOOGL": 175.00,  "MSFT": 420.00,
    "AMZN": 185.00,   "TSLA": 250.00,   "NVDA": 800.00,
    "META": 500.00,   "JPM": 195.00,    "V": 280.00,
    "NFLX": 600.00,
}

TICKER_PARAMS = {
    "AAPL": {"sigma": 0.22, "mu": 0.05},  # Moderate volatility
    "TSLA": {"sigma": 0.50, "mu": 0.03},  # High volatility, lower drift
    "NVDA": {"sigma": 0.40, "mu": 0.08},  # High vol + strong upward drift
    "JPM":  {"sigma": 0.18, "mu": 0.04},  # Low vol (bank)
    "V":    {"sigma": 0.17, "mu": 0.04},  # Low vol (payments)
    # ... etc.
}

DEFAULT_PARAMS = {"sigma": 0.25, "mu": 0.05}  # For dynamically-added tickers
```

Tickers added dynamically (via the watchlist API) receive the default params and a random seed price in the $50–$300 range.

---

## Code Structure

### `GBMSimulator` — the math engine

Pure computation, no I/O, no asyncio. Designed to be fast (called every 500ms).

```python
class GBMSimulator:
    # State
    _tickers: list[str]
    _prices: dict[str, float]          # Current price per ticker
    _params: dict[str, dict]           # mu/sigma per ticker
    _cholesky: np.ndarray | None       # Cholesky factor of correlation matrix
    _dt: float                         # Time step
    _event_prob: float                 # Shock event probability

    def step(self) -> dict[str, float]:
        """Hot path: advance all tickers one tick. Returns {ticker: price}."""

    def add_ticker(self, ticker: str) -> None:
        """Add ticker, rebuild Cholesky."""

    def remove_ticker(self, ticker: str) -> None:
        """Remove ticker, rebuild Cholesky."""

    def get_price(self, ticker: str) -> float | None:
        """Current price for a single ticker."""

    def get_tickers(self) -> list[str]:
        """List of tracked tickers."""
```

### `SimulatorDataSource` — the asyncio adapter

Wraps `GBMSimulator` to implement the `MarketDataSource` interface. Runs an asyncio background task.

```python
class SimulatorDataSource(MarketDataSource):
    # State
    _sim: GBMSimulator | None
    _cache: PriceCache
    _interval: float        # Seconds between ticks (default: 0.5)
    _task: asyncio.Task | None

    async def start(self, tickers: list[str]) -> None:
        # 1. Create GBMSimulator with all starting tickers
        # 2. Seed the cache immediately (so SSE has data on first connect)
        # 3. Launch _run_loop() as an asyncio background task

    async def stop(self) -> None:
        # Cancel the task, await CancelledError

    async def add_ticker(self, ticker: str) -> None:
        # Forward to simulator, seed cache with initial price immediately

    async def remove_ticker(self, ticker: str) -> None:
        # Remove from simulator, evict from cache

    async def _run_loop(self) -> None:
        # Every _interval seconds: call sim.step(), write all prices to cache
```

### The hot path — `_run_loop`

```python
async def _run_loop(self) -> None:
    while True:
        try:
            if self._sim:
                prices = self._sim.step()          # Pure math, microseconds
                for ticker, price in prices.items():
                    self._cache.update(ticker=ticker, price=price)  # Lock, write
        except Exception:
            logger.exception("Simulator step failed")  # Never crash the loop
        await asyncio.sleep(self._interval)        # Yield to event loop
```

`GBMSimulator.step()` is synchronous and fast enough (pure NumPy, microseconds) that it does **not** need `asyncio.to_thread()`. The event loop is blocked for less than a millisecond per tick.

---

## Startup Seeding

When `start()` is called, the simulator immediately seeds the cache with initial prices before starting the loop:

```python
async def start(self, tickers: list[str]) -> None:
    self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)

    # Seed the cache NOW — don't wait for the first loop iteration
    for ticker in tickers:
        price = self._sim.get_price(ticker)
        if price is not None:
            self._cache.update(ticker=ticker, price=price)

    self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")
```

This ensures SSE clients that connect immediately after startup receive real data, not empty responses.

---

## Adding Dynamically-Watched Tickers

When a user adds a new ticker to the watchlist:
1. `add_ticker()` is called on `SimulatorDataSource`
2. It calls `GBMSimulator.add_ticker()` which:
   - Assigns a seed price (from `SEED_PRICES` if known, else random $50–$300)
   - Assigns params (from `TICKER_PARAMS` if known, else `DEFAULT_PARAMS`)
   - Rebuilds the Cholesky matrix to include the new ticker
3. The new ticker's initial price is written to the cache immediately
4. On the next `step()`, the ticker participates in correlated GBM along with all others

---

## Performance Characteristics

| Metric | Value |
|---|---|
| Tick interval | 500ms |
| Step computation time | ~50–200 µs for 10 tickers (NumPy) |
| Memory per ticker | ~100 bytes (price + params dict) |
| Cholesky rebuild | ~50 µs for 10 tickers |
| Event loop blockage per tick | < 1 ms |
| Shock event frequency | ~1 per 50 seconds (10 tickers, 2 ticks/sec) |

The simulator has negligible performance impact on the FastAPI application.

---

## Behavioral Properties

- **Prices always positive**: GBM's exponential ensures prices stay positive. There is no floor or ceiling.
- **No mean reversion**: Pure GBM drifts; over long runs (hours), prices will wander from their seeds. For a demo app this is fine.
- **Reproducibility**: Not seeded — each run produces different price paths. Set `numpy.random.seed()` before `start()` for reproducible tests.
- **Scale-invariance**: The same code handles 1 ticker or 50 tickers identically.
- **No market hours**: The simulator runs 24/7. If deployed behind a real UI, you might add logic to freeze prices outside 9:30–16:00 ET.
