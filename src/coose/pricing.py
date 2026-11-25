"""Rough dollar estimates for Claude API usage.

Prices are US dollars per million tokens at Anthropic's first-party rates,
as of November 2026. They change over time, so treat the totals as
estimates; the Claude Console's billing page is the real bill.
"""

from __future__ import annotations

# model: (input, output, cache read)
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-fable-5-2": (8.00, 40.00, 0.20),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}

# Writing to the cache costs more than plain input, by how long it is kept.
CACHE_WRITE_MULTIPLIER = {"5m": 1.25, "1h": 2.0}


def estimate(model: str, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int,
             cache_ttl: str = "5m", cache_write_1h: int | None = None) -> float | None:
    """Estimated dollars for one call, or None for a model without a known price.

    `cache_write_1h` says how many of the cache writes were kept for an hour,
    when the API reported it; otherwise all writes are priced at `cache_ttl`."""
    price = PRICES.get(model)
    if price is None:
        return None
    inp, out, read = price
    if cache_write_1h is None:
        writes = cache_write * inp * CACHE_WRITE_MULTIPLIER.get(cache_ttl, 1.25)
    else:
        long = min(cache_write_1h, cache_write)
        writes = long * inp * CACHE_WRITE_MULTIPLIER["1h"] + (cache_write - long) * inp * CACHE_WRITE_MULTIPLIER["5m"]
    return (input_tokens * inp + output_tokens * out + cache_read * read + writes) / 1_000_000
