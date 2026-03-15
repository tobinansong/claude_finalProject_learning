"""FinAlly Market Data Simulator Demo.

Run with:  uv run market_data_demo.py

Displays a live-updating terminal dashboard of simulated stock prices
using the GBM simulator and Rich library. Runs 60 seconds or until Ctrl+C.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from app.market.cache import PriceCache
from app.market.seed_prices import SEED_PRICES
from app.market.simulator import SimulatorDataSource

# Sparkline characters, lowest to highest bar
_SPARK = "▁▂▃▄▅▆▇█"

# Ordered ticker list matching the default watchlist
TICKERS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]

DURATION = 60  # seconds


def sparkline(values: list[float], width: int = 38) -> str:
    """Render a sequence of price values as a unicode bar-chart sparkline."""
    if len(values) < 2:
        return ""
    # Use the last `width` values so the chart fills the column
    vals = list(values)[-width:]
    lo, hi = min(vals), max(vals)
    spread = hi - lo
    n = len(_SPARK) - 1
    if spread == 0:
        return _SPARK[n // 2] * len(vals)
    return "".join(_SPARK[round((v - lo) / spread * n)] for v in vals)


def build_table(cache: PriceCache, history: dict[str, deque]) -> Table:
    """Build the live-prices table."""
    table = Table(
        expand=True,
        border_style="bright_black",
        header_style="bold white",
        pad_edge=True,
        padding=(0, 1),
        show_edge=True,
    )
    table.add_column("Ticker", style="bold white", width=6, no_wrap=True)
    table.add_column("Price", justify="right", width=10, no_wrap=True)
    table.add_column("Change", justify="right", width=9, no_wrap=True)
    table.add_column("Chg %", justify="right", width=8, no_wrap=True)
    table.add_column("", width=2, no_wrap=True)       # direction arrow
    table.add_column("Sparkline", no_wrap=True)        # fills remaining width

    for ticker in TICKERS:
        update = cache.get(ticker)
        if update is None:
            table.add_row(ticker, "---", "---", "---", "", "")
            continue

        if update.direction == "up":
            color = "green"
            arrow = Text("▲", style="bold green")
        elif update.direction == "down":
            color = "red"
            arrow = Text("▼", style="bold red")
        else:
            color = "bright_black"
            arrow = Text("─", style="bright_black")

        price_str = Text(f"${update.price:,.2f}", style=color)
        change_str = Text(f"{update.change:+.2f}", style=color)
        pct_str = Text(f"{update.change_percent:+.2f}%", style=color)

        vals = list(history.get(ticker, []))
        spark_str = Text(sparkline(vals), style="cyan") if len(vals) > 1 else Text("")

        table.add_row(ticker, price_str, change_str, pct_str, arrow, spark_str)

    return table


def build_header(cache: PriceCache, start_time: float) -> Text:
    """Build the header status line."""
    elapsed = time.time() - start_time
    remaining = max(0.0, DURATION - elapsed)
    return Text.assemble(
        ("  FinAlly ", "bold yellow"),
        ("Market Data Simulator", "bold white"),
        ("  |  ", "bright_black"),
        (f"{elapsed:5.1f}s elapsed", "cyan"),
        ("  |  ", "bright_black"),
        (f"{remaining:4.1f}s remaining", "cyan"),
        ("  |  ", "bright_black"),
        (f"{len(cache)} tickers", "white"),
        ("  |  ", "bright_black"),
        ("Ctrl+C to exit", "bright_black italic"),
    )


def build_screen(
    cache: PriceCache,
    history: dict[str, deque],
    start_time: float,
) -> Layout:
    """Build the full terminal layout."""
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
    )
    layout["header"].update(
        Panel(build_header(cache, start_time), border_style="yellow", padding=(0, 0))
    )
    layout["body"].update(
        Panel(
            build_table(cache, history),
            title="[bold white]Live Prices[/]",
            border_style="bright_black",
        )
    )
    return layout


def print_summary(cache: PriceCache) -> None:
    """Print final session summary comparing to seed prices."""
    console = Console()
    console.print()
    console.print("[bold yellow]  FinAlly[/] [bold]Session Summary[/]")
    console.print()

    table = Table(border_style="bright_black", header_style="bold white", expand=False)
    table.add_column("Ticker", style="bold white", width=8)
    table.add_column("Seed Price", justify="right", width=12)
    table.add_column("Final Price", justify="right", width=12)
    table.add_column("Session Chg", justify="right", width=12)

    for ticker in TICKERS:
        seed = SEED_PRICES.get(ticker, 0)
        update = cache.get(ticker)
        if update is None:
            continue
        final = update.price
        pct = ((final - seed) / seed * 100) if seed else 0
        color = "green" if pct > 0 else ("red" if pct < 0 else "bright_black")
        table.add_row(
            ticker,
            f"${seed:,.2f}",
            f"[{color}]${final:,.2f}[/]",
            f"[{color}]{pct:+.2f}%[/]",
        )

    console.print(table)
    console.print()


async def run() -> None:
    """Main demo loop."""
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache, update_interval=0.5)
    history: dict[str, deque] = {t: deque(maxlen=60) for t in TICKERS}

    await source.start(TICKERS)
    start_time = time.time()

    # Seed initial history from the first snapshot
    for ticker in TICKERS:
        update = cache.get(ticker)
        if update:
            history[ticker].append(update.price)

    try:
        with Live(
            build_screen(cache, history, start_time),
            refresh_per_second=4,
            screen=True,
        ) as live:
            last_version = cache.version
            while time.time() - start_time < DURATION:
                await asyncio.sleep(0.25)

                if cache.version == last_version:
                    continue
                last_version = cache.version

                for ticker in TICKERS:
                    update = cache.get(ticker)
                    if update is not None:
                        history[ticker].append(update.price)

                live.update(build_screen(cache, history, start_time))

    except KeyboardInterrupt:
        pass
    finally:
        await source.stop()

    print_summary(cache)


if __name__ == "__main__":
    asyncio.run(run())
