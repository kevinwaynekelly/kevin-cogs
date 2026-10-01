"""Compare level lookup with the previous 5,000-threshold implementation.

Run from the repository root: python -m scripts.benchmark_levels
Results measure local calculation time, not Discord or storage latency.
"""

import json
import timeit
import tracemalloc

from levelplus.levels import cumulative_xp, level_from_xp


def previous_level(xp):
    thresholds, total = [0], 0.0
    for level in range(1, 5001):
        total += 83.2 + 100.433 * (level - 1)
        thresholds.append(int(round(total)))
    lo, hi = 0, len(thresholds) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if xp >= thresholds[mid]:
            lo = mid + 1
        else:
            hi = mid - 1
    return max(0, hi)


def current_level(xp):
    return level_from_xp(xp, "linear", 1.0, 0, 83.2, 100.433)


def measure_peak(fn):
    cumulative_xp.cache_clear()
    tracemalloc.start()
    fn(1000000)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak


def main():
    workloads = [0, 100, 1000, 10000, 1000000, 10000000]
    assert [previous_level(xp) for xp in workloads] == [current_level(xp) for xp in workloads]
    previous = min(
        timeit.repeat(lambda: [previous_level(xp) for xp in workloads], number=100, repeat=3)
    )
    current = min(
        timeit.repeat(lambda: [current_level(xp) for xp in workloads], number=100, repeat=3)
    )
    print(
        json.dumps(
            {
                "lookups_per_run": len(workloads) * 100,
                "previous_seconds": round(previous, 6),
                "current_seconds": round(current, 6),
                "speedup": round(previous / current, 1),
                "previous_peak_bytes": measure_peak(previous_level),
                "current_peak_bytes": measure_peak(current_level),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
