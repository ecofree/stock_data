"""Chip distribution (CYQ) calculator built from local daily kline history.

Standard algorithm (same as 通达信):
    For each trading day:
        turnover = volume / float_shares
        New chips are distributed across [low, high] using triangular weighting
        (peak at avg price).  All existing chips decay by (1 - turnover).

    After processing all days, the result is a price -> chip_volume mapping
    from which we derive:
        - winner_pct: % of chips below current close
        - cost_avg: volume-weighted average cost
        - scr_90: 90% chip concentration ratio
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ChipResult:
    """Analysis output for one stock's chip distribution."""
    trade_date: str
    current_price: float
    winner_pct: float          # 获利盘比例 0..100
    cost_avg: float            # 平均成本
    scr_90: float              # 90%集中度 (lower = more concentrated)
    p5_price: float
    p95_price: float
    peak_price: float          # 最大筹码峰对应价格
    total_chips: float
    profile: list[tuple[float, float]]  # (price_bin, cumulative_volume)


def _triangular_weight(price: float, low: float, high: float,
                       avg: float) -> float:
    """Triangular distribution weight; peaks at avg, zero at extremes."""
    if high <= low or avg < low or avg > high:
        return 1.0
    dist_from_avg = abs(price - avg)
    max_dist = max(avg - low, high - avg)
    if max_dist == 0:
        return 1.0
    return max(0.0, 1.0 - dist_from_avg / max_dist)


def calculate_chip_distribution(
    bars: list[dict],
    num_bins: int = 60,
    decay_base: float = 1.0,
) -> ChipResult:
    """Calculate CYQ from a chronologically ordered list of daily bars.

    Args:
        bars: [{date, open, high, low, close, volume}, ...] oldest first.
        num_bins: number of price bins for the final distribution.
        decay_base: multiplier applied to turnover for chip decay speed.
            Higher = faster forgetting of old chips.

    Returns:
        ChipResult with full profile and summary statistics.
    """
    if not bars:
        raise ValueError("bars list is empty")

    # Determine global price range across all bars
    all_lows = [b["low"] for b in bars if b.get("low")]
    all_highs = [b["high"] for b in bars if b.get("high")]
    if not all_lows or not all_highs:
        raise ValueError("no valid price data")

    global_min = min(all_lows)
    global_max = max(all_highs)
    price_span = global_max - global_min
    if price_span <= 0:
        price_span = global_max * 0.01
    bin_size = price_span / num_bins

    # Initialize uniform chip distribution
    bins = [0.0] * num_bins
    for i in range(num_bins):
        bins[i] = 1.0 / num_bins

    latest = bars[-1]
    current_price = latest["close"]

    # Process each day to evolve the chip distribution
    for bar in bars:
        lo, hi = bar.get("low", 0), bar.get("high", 0)
        vol = bar.get("volume", 0) or 0
        if hi <= lo or vol <= 0:
            continue

    # Simpler & more robust approach: accumulate chips weighted by recency
    n = len(bars)
    profile: dict[int, float] = {}
    for idx, bar in enumerate(bars):
        lo = bar.get("low", 0) or 0
        hi = bar.get("high", 0) or 0
        vol = bar.get("volume", 0) or 0
        close = bar.get("close", 0) or 0
        if hi <= lo or vol <= 0 or close <= 0:
            continue

        # Recency weight: recent bars matter more
        recency = (idx + 1) / n  # 0..1, higher = more recent

        # Distribute volume across [lo, hi] with triangular weight centered at close
        bin_start = int((lo - global_min) / bin_size)
        bin_end = int((hi - global_min) / bin_size)
        bin_start = max(0, min(bin_start, num_bins - 1))
        bin_end = max(0, min(bin_end, num_bins - 1))
        span = max(1, bin_end - bin_start)

        weight_per_bin = vol / span * (0.3 + 0.7 * recency)
        for b in range(bin_start, bin_end + 1):
            tri = _triangular_weight(
                global_min + (b + 0.5) * bin_size, lo, hi, close)
            profile[b] = profile.get(b, 0) + weight_per_bin * tri

    if not profile:
        raise ValueError("no chips accumulated")

    # Build sorted (price, volume) pairs
    sorted_bins = sorted(profile.items())
    total_chips = sum(v for _, v in sorted_bins)
    if total_chips <= 0:
        raise ValueError("total chips is zero")

    # Convert to price-volume pairs
    price_vol: list[tuple[float, float]] = []
    for bin_idx, vol in sorted_bins:
        px = global_min + (bin_idx + 0.5) * bin_size
        price_vol.append((round(px, 2), round(vol, 4)))

    # Calculate winner ratio (% of chips below current price)
    cum_below = sum(v for px, v in price_vol if px <= current_price)
    winner_pct = round(cum_below / total_chips * 100, 1)

    # Average cost
    cost_avg = round(sum(px * v for px, v in price_vol) / total_chips, 2)

    # 90% concentration: find P5 and P95 prices
    cum = 0.0
    p5 = p95 = current_price
    for px, v in price_vol:
        cum += v
        if cum >= total_chips * 0.05:
            p5 = px
            break
    cum = 0.0
    for px, v in reversed(price_vol):
        cum += v
        if cum >= total_chips * 0.05:
            p95 = px
            break

    scr_90 = round((p95 - p5) / (p95 + p5) * 100, 1) if (p95 + p5) > 0 else 50.0

    # Peak price
    peak_bin = max(profile, key=profile.get)
    peak_price = round(global_min + (peak_bin + 0.5) * bin_size, 2)

    return ChipResult(
        trade_date=latest.get("date", ""),
        current_price=current_price,
        winner_pct=winner_pct,
        cost_avg=cost_avg,
        scr_90=scr_90,
        p5_price=p5,
        p95_price=p95,
        peak_price=peak_price,
        total_chips=round(total_chips, 2),
        profile=price_vol,
    )
