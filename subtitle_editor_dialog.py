"""可视化 SRT 字幕校对与编辑器。

该模块保持独立，不依赖主窗口状态。主窗口可以在用户选择（或当前工作流
已经生成）字幕文件后创建 :class:`SubtitleEditorDialog`，然后调用
``dialog.exec_()``。解析和写回逻辑也提供了无界面的函数，方便测试以及其它
工作流复用::

    entries = parse_srt_file("captions.srt")
    write_srt_file("captions.srt", entries)

SRT 的序号和时间轴在表格中只读，字幕内容使用 ``QPlainTextEdit`` 编辑器，因此
中英双语字幕中的换行可以直接保留。写文件采用同目录临时文件加原子替换，
避免程序在写入过程中退出而破坏原字幕文件。
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Union

from PyQt5.QtCore import QAbstractItemModel, QModelIndex, Qt
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


SrtEntry = Dict[str, Any]

# SRT 时间轴允许一位小时（常见格式），毫秒分隔符同时接受逗号和句点。
_TIMING_RE = re.compile(
    r"^\s*(?P<start>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*"
    r"-->\s*(?P<end>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})"
    r"(?:\s+.*)?$"
)
_INDEX_RE = re.compile(r"^\s*\d+\s*$")


class SrtParseError(ValueError):
    """字幕格式错误，包含可直接显示给用户的提示。"""


def _decode_srt(raw: bytes, encoding: Optional[str] = None) -> tuple[str, str]:
    """解码字幕，并返回 ``(文本, 实际编码)``。

    生成字幕一般是 UTF-8；为了兼容旧字幕，读取失败时依次尝试 GB18030 和
    Windows-1252。显式传入 ``encoding`` 时只使用该编码，错误会原样抛出。
    """

    candidates = [encoding] if encoding else ["utf-8-sig", "utf-8", "gb18030", "cp1252"]
    last_error: Optional[UnicodeDecodeError] = None
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return raw.decode(candidate), candidate
        except UnicodeDecodeError as exc:
            last_error = exc
    if last_error is not None:
        raise SrtParseError(f"字幕文件不是受支持的文本编码（尝试了 UTF-8/GB18030）：{last_error}")
    raise SrtParseError("无法读取字幕文件编码。")


def _normalize_timing(timing: str) -> str:
    """统一时间轴两端空白，同时保留毫秒分隔符和原始精度。"""

    match = _TIMING_RE.match(timing)
    if not match:
        raise SrtParseError(f"无效的时间轴：{timing!r}（应为 HH:MM:SS,mmm --> HH:MM:SS,mmm）")
    # 时间轴后的可选设置（例如位置标签）对编辑器没有意义，保留它们可避免
    # 用户打开后无意间丢失文件信息。
    return timing.strip()


def parse_srt_text(text: str) -> List[SrtEntry]:
    """将 SRT 文本解析为 ``[{index, time, text}, ...]``。

    解析器接受 UTF-8 BOM、CRLF 换行、块之间多余空行，以及缺少序号的字幕
    块（此时自动使用顺序序号）。时间轴必须存在；错误会抛出
    :class:`SrtParseError`，异常信息包含块号，便于在 GUI 中定位问题。
    """

    if not isinstance(text, str):
        raise TypeError("SRT 内容必须是字符串。")
    normalized = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    # ``split`` 而不是 ``split('\\n\\n')``，这样三个或更多空行也能正确分块。
    blocks = [block for block in re.split(r"\n\s*\n", normalized.strip()) if block.strip()]
    entries: List[SrtEntry] = []
    for block_number, block in enumerate(blocks, 1):
        lines = block.split("\n")
        timing_index = next((i for i, line in enumerate(lines) if _TIMING_RE.match(line)), None)
        if timing_index is None:
            preview = " ".join(line.strip() for line in lines[:2])
            raise SrtParseError(f"第 {block_number} 段缺少有效时间轴：{preview[:120]}")

        timing = _normalize_timing(lines[timing_index])
        index: int
        if timing_index > 0 and _INDEX_RE.match(lines[timing_index - 1]):
            index = int(lines[timing_index - 1].strip())
        else:
            index = len(entries) + 1

        # 时间轴之后的所有行都属于字幕内容；不要 strip 每一行，以保留双语
        # 文本及其换行。去除块末尾空白行即可。
        content_lines = lines[timing_index + 1 :]
        while content_lines and not content_lines[-1].strip():
            content_lines.pop()
        subtitle_text = "\n".join(content_lines)
        entries.append({"index": index, "time": timing, "text": subtitle_text})
    return entries


def parse_srt_file(path: Union[str, os.PathLike[str]], encoding: Optional[str] = None) -> List[SrtEntry]:
    """读取并解析 SRT 文件。"""

    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"字幕文件不存在：{file_path}")
    try:
        raw = file_path.read_bytes()
    except OSError as exc:
        raise OSError(f"无法读取字幕文件：{file_path}\n{exc}") from exc
    text, _ = _decode_srt(raw, encoding)
    return parse_srt_text(text)


def _entry_value(entry: Union[SrtEntry, Mapping[str, Any]], *keys: str, default: Any = "") -> Any:
    for key in keys:
        if key in entry:
            return entry[key]
    return default


def serialize_srt(entries: Iterable[Mapping[str, Any]]) -> str:
    """把字幕记录序列化为标准 SRT 文本（UTF-8，末尾换行）。"""

    blocks: List[str] = []
    for position, entry in enumerate(entries, 1):
        raw_index = _entry_value(entry, "index", "序号", default=position)
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            index = position
        if index < 0:
            index = position

        timing = str(_entry_value(entry, "time", "timing", "时间轴", default="")).strip()
        if not timing or not _TIMING_RE.match(timing):
            raise SrtParseError(f"第 {position} 条字幕的时间轴无效：{timing!r}")
        content = str(_entry_value(entry, "text", "content", "字幕内容", default=""))
        content = content.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
        # 空字幕仍保留一个空行，保证时间轴和下一块不会粘连。
        blocks.append(f"{index}\n{timing}\n{content}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def write_srt_file(
    path: Union[str, os.PathLike[str]],
    entries: Iterable[Mapping[str, Any]],
    encoding: str = "utf-8",
) -> str:
    """原子写回字幕文件并返回最终路径。"""

    file_path = Path(path)
    if file_path.suffix.lower() != ".srt":
        raise ValueError("字幕文件必须使用 .srt 扩展名。")
    file_path.parent.mkdir(parents=True, exist_ok=True)
    text = serialize_srt(entries)
    temp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding=encoding, newline="\n", dir=str(file_path.parent),
            prefix=f".{file_path.stem}.", suffix=".tmp", delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, file_path)
    except (OSError, UnicodeError) as exc:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
        raise OSError(f"保存字幕失败：{file_path}\n{exc}") from exc
    return str(file_path)


class _MultilineTextDelegate(QStyledItemDelegate):
    """让表格的字幕内容单元格使用可输入换行的 QPlainTextEdit。"""

    def createEditor(self, parent: QWidget, option: Any, index: QModelIndex) -> QWidget:
        editor = QPlainTextEdit(parent)
        editor.setFrameStyle(0)
        editor.setTabChangesFocus(False)
        editor.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        editor.setFont(parent.font())
        editor.setMinimumHeight(max(42, option.rect.height))
        return editor

    def setEditorData(self, editor: QWidget, index: QModelIndex) -> None:
        if isinstance(editor, QPlainTextEdit):
            editor.setPlainText(str(index.data(Qt.EditRole) or ""))
            editor.selectAll()

    def setModelData(
        self, editor: QWidget, model: QAbstractItemModel, index: QModelIndex
    ) -> None:
        if isinstance(editor, QPlainTextEdit):
            model.setData(index, editor.toPlainText(), Qt.EditRole)

    def updateEditorGeometry(self, editor: QWidget, option: Any, index: QModelIndex) -> None:
        editor.setGeometry(option.rect)


class SubtitleEditorDialog(QDialog):
    """字幕审阅与校对对话框。

    ``subtitle_path`` 可以直接传入当前工作流的字幕路径；为空时会弹出文件
    选择框。传入路径无效或解析失败时，错误会通过友好提示框显示，并保持对
    话框可关闭，不会让主程序崩溃。
    """

    def __init__(
        self,
        subtitle_path: Optional[Union[str, os.PathLike[str]]] = None,
        parent: Optional[QWidget] = None,
        entries: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("审阅与校对字幕")
        self.setMinimumSize(800, 600)
        self.resize(900, 680)
        self.subtitle_path = ""
        self._loaded = False

        self.path_label = QLabel("尚未选择字幕文件")
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(["序号", "时间轴", "字幕内容"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(
            QAbstractItemView.DoubleClicked
            | QAbstractItemView.EditKeyPressed
            | QAbstractItemView.SelectedClicked
        )
        self.table.setWordWrap(True)
        self.table.setAlternatingRowColors(True)
        self.table.setItemDelegateForColumn(2, _MultilineTextDelegate(self.table))
        self.table.itemChanged.connect(self._on_item_changed)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setMinimumSectionSize(70)
        header.resizeSection(0, 70)
        header.resizeSection(1, 255)
        self.table.verticalHeader().setDefaultSectionSize(48)
        self.table.verticalHeader().setMinimumSectionSize(32)

        self.save_button = QPushButton("保存修改")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.save_changes)
        self.save_as_button = QPushButton("另存为…")
        self.save_as_button.clicked.connect(self.save_as)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.reject)

        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        button_layout.addWidget(self.save_button)
        button_layout.addWidget(self.save_as_button)
        button_layout.addWidget(self.cancel_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.path_label)
        layout.addWidget(self.table, 1)
        layout.addLayout(button_layout)

        if entries is not None:
            self._populate_table(entries)
            self._loaded = True
        elif subtitle_path:
            self.load_file(subtitle_path)
        else:
            # 选择框放在窗口初始化完成后调用，避免某些平台上父窗口尚未
            # 完成创建导致文件对话框出现在错误的位置。
            from PyQt5.QtCore import QTimer

            QTimer.singleShot(0, self._choose_file)

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择要校对的字幕", "", "SRT 字幕文件 (*.srt);;所有文件 (*)"
        )
        if path:
            self.load_file(path)
        else:
            self.reject()

    def load_file(self, path: Union[str, os.PathLike[str]]) -> bool:
        """加载字幕并刷新表格；失败时显示提示并返回 ``False``。"""

        try:
            parsed = parse_srt_file(path)
        except (FileNotFoundError, OSError, SrtParseError, TypeError) as exc:
            QMessageBox.warning(self, "无法打开字幕", str(exc))
            self.save_button.setEnabled(False)
            self.save_as_button.setEnabled(False)
            self._loaded = False
            return False
        self.subtitle_path = str(Path(path))
        self.path_label.setText(f"文件：{self.subtitle_path}")
        self._populate_table(parsed)
        self.save_button.setEnabled(True)
        self.save_as_button.setEnabled(True)
        self._loaded = True
        return True

    def _populate_table(self, entries: Sequence[Mapping[str, Any]]) -> None:
        self.table.setRowCount(0)
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            index = str(_entry_value(entry, "index", "序号", default=row + 1))
            timing = str(_entry_value(entry, "time", "timing", "时间轴", default=""))
            content = str(_entry_value(entry, "text", "content", "字幕内容", default=""))
            index_item = QTableWidgetItem(index)
            time_item = QTableWidgetItem(timing)
            content_item = QTableWidgetItem(content)
            index_item.setFlags(index_item.flags() & ~Qt.ItemIsEditable)
            time_item.setFlags(time_item.flags() & ~Qt.ItemIsEditable)
            index_item.setTextAlignment(Qt.AlignCenter)
            time_item.setTextAlignment(Qt.AlignVCenter | Qt.AlignLeft)
            content_item.setTextAlignment(Qt.AlignVCenter | Qt.AlignLeft)
            self.table.setItem(row, 0, index_item)
            self.table.setItem(row, 1, time_item)
            self.table.setItem(row, 2, content_item)
        self.table.resizeRowsToContents()
        for row in range(self.table.rowCount()):
            self.table.setRowHeight(row, max(42, self.table.rowHeight(row)))

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        """编辑多行字幕后立即调整行高，避免内容被单元格裁剪。"""

        if item.column() != 2:
            return
        row = item.row()
        self.table.resizeRowToContents(row)
        self.table.setRowHeight(row, max(42, self.table.rowHeight(row)))

    def _collect_entries(self) -> List[SrtEntry]:
        entries: List[SrtEntry] = []
        for row in range(self.table.rowCount()):
            index_item = self.table.item(row, 0)
            time_item = self.table.item(row, 1)
            content_item = self.table.item(row, 2)
            if time_item is None or not time_item.text().strip():
                raise SrtParseError(f"第 {row + 1} 行缺少时间轴。")
            entries.append(
                {
                    "index": index_item.text().strip() if index_item else row + 1,
                    "time": time_item.text().strip(),
                    "text": content_item.text() if content_item else "",
                }
            )
        return entries

    def _save_to(self, path: str) -> bool:
        try:
            entries = self._collect_entries()
            write_srt_file(path, entries)
        except (OSError, SrtParseError, ValueError, TypeError) as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return False
        self.subtitle_path = str(Path(path))
        self.path_label.setText(f"文件：{self.subtitle_path}")
        QMessageBox.information(self, "保存成功", "字幕修改已保存。")
        self.accept()
        return True

    def save_changes(self) -> bool:
        """覆盖当前文件，成功后关闭面板。"""

        if not self.subtitle_path:
            return self.save_as()
        return self._save_to(self.subtitle_path)

    def save_as(self) -> bool:
        """选择新的 ``*_edited.srt`` 路径并保存。"""

        default = self.subtitle_path
        if default:
            source = Path(default)
            default = str(source.with_name(f"{source.stem}_edited.srt"))
        path, _ = QFileDialog.getSaveFileName(
            self, "另存字幕", default, "SRT 字幕文件 (*.srt);;所有文件 (*)"
        )
        if not path:
            return False
        if Path(path).suffix.lower() != ".srt":
            path += ".srt"
        return self._save_to(path)


# 兼容常见调用命名，并让外部脚本可以把 ``parse_srt`` 当作“读取文件”
# 或“解析文本”函数使用。PathLike 始终按文件处理；普通字符串若是现有文件
# 路径也按文件处理，其余字符串视为 SRT 文本。
def parse_srt(source: Union[str, os.PathLike[str]]) -> List[SrtEntry]:
    if isinstance(source, os.PathLike):
        return parse_srt_file(source)
    if isinstance(source, str) and "\n" not in source and "\r" not in source:
        try:
            if Path(source).is_file():
                return parse_srt_file(source)
        except OSError:
            # 过长或非法路径按文本继续解析，最终会给出格式错误提示。
            pass
    return parse_srt_text(source)


write_srt = write_srt_file


__all__ = [
    "SrtEntry",
    "SrtParseError",
    "SubtitleEditorDialog",
    "parse_srt",
    "parse_srt_file",
    "parse_srt_text",
    "serialize_srt",
    "write_srt",
    "write_srt_file",
]
