from rightsize.units import format_cpu, format_memory, parse_cpu, parse_memory


def test_parse_and_format_round_trip():
    assert parse_cpu("250m") == 0.25
    assert parse_cpu("2") == 2.0
    assert parse_memory("64Mi") == 64 * 1024 * 1024
    assert parse_memory("1G") == 1e9
    assert format_cpu(0.0101) == "11m"
    assert format_memory(64 * 1024 * 1024) == "64Mi"
    assert format_memory(64 * 1024 * 1024 + 1) == "65Mi"
    assert format_cpu(None) is None
