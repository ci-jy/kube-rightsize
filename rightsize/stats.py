"""Small, dependency-free statistics helpers."""

import math


def percentile(values, q):
    """Percentile with linear interpolation between closest ranks.

    Matches numpy's default ("linear") method, so P50 of [1, 2, 3, 4] is 2.5.
    ``q`` is in [0, 100].
    """
    if not values:
        raise ValueError("percentile of empty sequence")
    if not 0 <= q <= 100:
        raise ValueError("q must be within [0, 100]")
    data = sorted(values)
    if len(data) == 1:
        return float(data[0])
    pos = (len(data) - 1) * q / 100
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(data[lo])
    frac = pos - lo
    return data[lo] + (data[hi] - data[lo]) * frac
