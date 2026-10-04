"""字幕校对编辑器的无交互解析/写回回归测试。"""

from pathlib import Path

import pytest


# GUI 依赖由 requirements.txt 提供；在只运行纯业务测试的环境中允许跳过本文件。
pytest.importorskip("PyQt5")

from subtitle_editor_dialog import (  # noqa: E402
    SrtParseError,
    SubtitleEditorDialog,
    parse_srt_file,
    parse_srt_text,
    serialize_srt,
    write_srt_file,
)
from PyQt5.QtCore import Qt  # noqa: E402


@pytest.fixture(scope="session")
def qt_app():
    """Provide a QApplication for the in-memory editor tests.

    The editor module is also used by the headless parser tests, so keeping the
    Qt fixture local to the tests that construct a dialog lets those tests run
    in CI with ``QT_QPA_PLATFORM=offscreen`` and remain skipped when PyQt5 is
    unavailable.
    """

    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return app


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


def test_in_memory_entries_are_loaded_without_creating_a_source_file(qt_app, tmp_path):
    """Machine translation results can be reviewed before any file is written."""

    entries = [
        {
            "index": 1,
            "time": "00:00:01,000 --> 00:00:03,000",
            "original_text": "机器翻译",
            "translated_text": "Machine translation",
        }
    ]
    suggested = tmp_path / "video_translated.srt"

    dialog = SubtitleEditorDialog(entries=entries, default_output_path=suggested)
    try:
        assert dialog.is_loaded
        assert dialog.subtitle_path == ""
        assert dialog.table.rowCount() == 1
        assert dialog.table.columnCount() == 4
        assert dialog.table.item(0, 2).text() == "机器翻译"
        assert dialog.table.item(0, 3).text() == "Machine translation"
        assert not (dialog.table.item(0, 2).flags() & Qt.ItemIsEditable)
        assert dialog.table.item(0, 3).flags() & Qt.ItemIsEditable
        assert not suggested.exists()
    finally:
        dialog.close()


def test_in_memory_editor_saves_only_after_user_confirmation(qt_app, tmp_path, monkeypatch):
    """Edited in-memory rows are serialized to SRT at the explicit save step."""

    entries = [
        {
            "index": 1,
            "time": "00:00:00,000 --> 00:00:01,500",
            "original_text": "原文",
            "translated_text": "Original",
        },
        {
            "index": 2,
            "time": "00:00:02,000 --> 00:00:03,500",
            "original_text": "第二句",
            "translated_text": "Second sentence",
        },
    ]
    target = tmp_path / "translated.srt"
    dialog = SubtitleEditorDialog(entries=entries)
    # Avoid a modal message box while exercising the dialog's save path.
    monkeypatch.setattr(
        "subtitle_editor_dialog.QMessageBox.information",
        staticmethod(lambda *args, **kwargs: None),
    )
    monkeypatch.setattr(
        "subtitle_editor_dialog.QFileDialog.getSaveFileName",
        staticmethod(lambda *args, **kwargs: (str(target), "SRT 字幕文件 (*.srt)")),
    )
    try:
        # 原文列仅作对照，用户修改第 4 列译文/双语内容。
        dialog.table.item(0, 3).setText("校对后的第一句\nCorrected first line")
        assert not target.exists()
        # ``save_changes`` is the same slot used by the visible 保存修改 button.
        # In memory mode it opens a save dialog and never overwrites the source.
        assert dialog.save_changes()
        assert target.exists()
        assert dialog.saved is True
        assert dialog.subtitle_path == str(target)
        assert parse_srt_file(target) == [
            {
                "index": 1,
                "time": "00:00:00,000 --> 00:00:01,500",
                "text": "校对后的第一句\nCorrected first line",
            },
            {
                "index": 2,
                "time": "00:00:02,000 --> 00:00:03,500",
                "text": "Second sentence",
            },
        ]
    finally:
        dialog.close()


def test_translation_thread_emits_memory_payload_without_writing_srt(tmp_path, monkeypatch):
    """The translation worker must hand off entries and leave disk untouched."""

    from main import TranslateSubtitleThread

    source = tmp_path / "source.srt"
    source.write_text(
        "1\n00:00:00,000 --> 00:00:01,000\n原文\n",
        encoding="utf-8",
    )
    translated = "1\n00:00:00,000 --> 00:00:01,000\nTranslation\n原文\n"

    monkeypatch.setattr("main.get_token", lambda: "test-token")
    monkeypatch.setattr("main.read_subtitle_file", lambda _path: source.read_text(encoding="utf-8"))

    def fake_translate(*args, **kwargs):
        return translated

    monkeypatch.setattr("main.translate_text", fake_translate)
    payloads = []
    errors = []
    worker = TranslateSubtitleThread(str(source), "en", include_original=True)
    worker.signal.connect(payloads.append)
    worker.error_signal.connect(errors.append)
    worker.run()

    assert errors == []
    assert payloads and payloads[0]["entries"][0] == {
        "index": 1,
        "time": "00:00:00,000 --> 00:00:01,000",
        "original_text": "原文",
        "translated_text": "Translation\n原文",
    }
    assert payloads[0]["default_output_path"].endswith("source_translated.srt")
    assert not (tmp_path / "source-zh&en.srt").exists()
