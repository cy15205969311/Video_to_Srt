"""字幕校对编辑器的无交互解析/写回回归测试。"""

from pathlib import Path

import pytest


# GUI 依赖由 requirements.txt 提供；在只运行纯业务测试的环境中允许跳过本文件。
pytest.importorskip("PyQt5")

from subtitle_editor_dialog import (  # noqa: E402
    SrtParseError,
    parse_srt_file,
    parse_srt_text,
    serialize_srt,
    write_srt_file,
)


def test_parse_bom_crlf_and_multiline_bilingual_text():
    source = (
        "\ufeff1\r\n"
        "00:00:01,000 --> 00:00:03,500\r\n"
        "中文\r\n"
        "English\r\n\r\n"
        "2\r\n"
        "00:00:04,000 --> 00:00:05,000\r\n"
        "下一句\r\n"
    )

    entries = parse_srt_text(source)

    assert entries == [
        {
            "index": 1,
            "time": "00:00:01,000 --> 00:00:03,500",
            "text": "中文\nEnglish",
        },
        {
            "index": 2,
            "time": "00:00:04,000 --> 00:00:05,000",
            "text": "下一句",
        },
    ]


def test_write_and_read_round_trip_with_gb18030(tmp_path: Path):
    path = tmp_path / "captions.srt"
    entries = [
        {
            "index": 1,
            "time": "00:00:00,000 --> 00:00:01,000",
            "text": "你好\nHello",
        }
    ]

    write_srt_file(path, entries, encoding="gb18030")

    assert parse_srt_file(path)[0]["text"] == "你好\nHello"
    assert serialize_srt(entries).endswith("\n")


def test_invalid_timing_reports_a_parse_error():
    with pytest.raises(SrtParseError, match="时间轴"):
        parse_srt_text("1\n00:00:01 --> 00:00:02\n缺少毫秒格式")
