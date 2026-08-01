"""Kubernetes quantity parsing and formatting for CPU and memory."""

import math

MI = 1024 * 1024

_MEM_SUFFIX = {
    "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4,
    "k": 1000, "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4,
}


def parse_cpu(q):
    """'250m' -> 0.25, '2' -> 2.0 (cores)."""
    if isinstance(q, (int, float)):
        return float(q)
    q = q.strip()
    if q.endswith("m"):
        return float(q[:-1]) / 1000
    return float(q)


def parse_memory(q):
    """'64Mi' -> 67108864 (bytes)."""
    if isinstance(q, (int, float)):
        return float(q)
    q = q.strip()
    for suffix in sorted(_MEM_SUFFIX, key=len, reverse=True):
        if q.endswith(suffix):
            return float(q[: -len(suffix)]) * _MEM_SUFFIX[suffix]
    return float(q)


def millicores(cores):
    return int(math.ceil(round(cores * 1000, 6)))


def format_cpu(cores):
    if cores is None:
        return None
    return f"{millicores(cores)}m"


def format_memory(nbytes):
    if nbytes is None:
        return None
    return f"{int(math.ceil(round(nbytes / MI, 6)))}Mi"


def round_up(value, step):
    """Round value up to the next multiple of step."""
    return math.ceil(round(value / step, 9)) * step
