from src.ffmpeg.timestamp import (
    build_final_filename,
    even_subsample_indices,
    format_timestamp,
    parse_showinfo_stderr,
)


def test_format_timestamp_zero():
    assert format_timestamp(0.0) == "00-00-00-000"


def test_format_timestamp_example():
    # 83.47s = 83470ms -> 00:01:23.470
    assert format_timestamp(83.47) == "00-01-23-470"


def test_format_timestamp_hours():
    assert format_timestamp(3723.5) == "01-02-03-500"


def test_format_timestamp_negative_clamped():
    assert format_timestamp(-5.0) == "00-00-00-000"


def test_build_final_filename_matches_example():
    # GX012080 + 83.47s (=83470ms) + index 23 -> GX012080-00-01-23-470-0023.jpg
    name = build_final_filename("GX012080", 83.47, 23)
    assert name == "GX012080-00-01-23-470-0023.jpg"


def test_build_final_filename_index_padding():
    name = build_final_filename("clip", 1.0, 5)
    assert name == "clip-00-00-01-000-0005.jpg"


def test_parse_showinfo_stderr_extracts_pts_times():
    sample = """[Parsed_showinfo_0 @ 0xaaa]   n:   0 pts: 0 pts_time:0.000
[Parsed_showinfo_0 @ 0xaaa]   n:   1 pts: 2002 pts_time:2.002
[Parsed_showinfo_0 @ 0xaaa]   n:   2 pts: 4004 pts_time:4.004
"""
    times = parse_showinfo_stderr(sample)
    assert times == [0.0, 2.002, 4.004]


def test_parse_showinfo_ignores_non_showinfo_lines():
    sample = """frame=    1 fps=0.0 q=5.0 size=       0kB time=00:00:00.00
[Parsed_showinfo_0 @ 0xaaa]   n:   0 pts: 0 pts_time:1.500
some other line
"""
    times = parse_showinfo_stderr(sample)
    assert times == [1.5]


def test_even_subsample_returns_all_when_under_target():
    assert even_subsample_indices(5, 10) == [0, 1, 2, 3, 4]


def test_even_subsample_deterministic():
    idxs = even_subsample_indices(100, 80)
    assert len(idxs) == 80
    assert idxs == sorted(idxs)
    assert 0 in idxs
    assert 99 in idxs


def test_even_subsample_target_one():
    assert even_subsample_indices(10, 1) == [5]


def test_even_subsample_zero_total():
    assert even_subsample_indices(0, 10) == []