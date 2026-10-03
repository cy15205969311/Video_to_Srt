"""富文本字幕样式设置与硬字幕导出面板。

这个模块只负责 PyQt5 界面和交互，实际的 SRT -> ASS 转换及 FFmpeg
压制由 :mod:`subtitle_export` 提供。主窗口可以这样打开面板::

    from subtitle_style_dialog import SubtitleStyleDialog
    dialog = SubtitleStyleDialog(self)
    dialog.exec_()

也可以在创建时传入已经选好的视频、SRT 路径，避免用户重复选择。面板
不会在主线程中执行 FFmpeg；``HardSubtitleWorker`` 会在 QThread 中工作，
并通过 progress/status/finished/error 信号回传状态。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFontDatabase
from PyQt5.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

try:
    from playback_state import playback_state_values
except ImportError:  # pragma: no cover - 仅供独立复制面板时使用
    playback_state_values = None  # type: ignore

# subtitle_export.py / ffmpeg_worker.py 由核心实现提供。这里集中导入，
# 方便未来替换核心模块，也避免把 FFmpeg 进程放在 GUI 线程。
try:
    from subtitle_export import SubtitleConfig  # type: ignore
    try:
        from subtitle_export import derive_output_path  # type: ignore
    except ImportError:
        derive_output_path = None  # type: ignore
    try:
        from ffmpeg_worker import HardSubtitleWorker  # type: ignore
    except ImportError:
        from subtitle_export import HardSubtitleWorker  # type: ignore
except ImportError:  # pragma: no cover - 仅在核心模块尚未安装时提供清晰错误
    HardSubtitleWorker = None  # type: ignore
    SubtitleConfig = None  # type: ignore
    derive_output_path = None  # type: ignore


class ColorButton(QPushButton):
    """显示当前颜色并在点击时弹出 QColorDialog 的小组件。"""

    colorChanged = pyqtSignal(str)

    def __init__(self, color: str = "#FFFFFF", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._color = "#FFFFFF"
        self.setMinimumWidth(96)
        self.clicked.connect(self._choose_color)
        self.set_color(color)

    @property
    def color(self) -> str:
        return self._color

    def set_color(self, color: str) -> None:
        qcolor = QColor(str(color))
        if not qcolor.isValid():
            qcolor = QColor("#FFFFFF")
        normalized = qcolor.name().upper()
        changed = normalized != self._color
        self._color = normalized
        # 同时设置文字和背景，让没有颜色预览能力的主题也可用。
        text_color = "#000000" if qcolor.lightness() > 150 else "#FFFFFF"
        self.setText(normalized)
        self.setStyleSheet(
            "QPushButton { background-color: %s; color: %s; border: 1px solid #888; "
            "border-radius: 3px; padding: 3px 8px; }" % (normalized, text_color)
        )
        if changed:
            self.colorChanged.emit(normalized)

    # 用 Qt 常见命名提供给主窗口复用。
    setColor = set_color

    def _choose_color(self) -> None:
        chosen = QColorDialog.getColor(QColor(self._color), self, "选择颜色")
        if chosen.isValid():
            self.set_color(chosen.name())


def _new_config(config: Any = None) -> Any:
    """创建核心 SubtitleConfig，同时兼容 dataclass 和普通配置类。"""

    if config is not None:
        return config
    if SubtitleConfig is None:
        raise ImportError("缺少 subtitle_export.py，请先添加字幕导出核心模块")
    try:
        return SubtitleConfig()
    except TypeError:
        # 核心模块若要求字典参数，提供产品文档中的默认值。
        defaults = {
            "font_name": "Microsoft YaHei",
            "font_size": 24,
            "primary_color": "#FFFFFF",
            "outline_width": 2,
            "outline_color": "#000000",
            "background_enabled": False,
            "background_color": "#000000",
            "background_opacity": 160,
            "position": "bottom",
            "x_offset": 0,
            "y_offset": 0,
        }
        try:
            return SubtitleConfig(**defaults)
        except TypeError:
            return SubtitleConfig(defaults)


def _get(config: Any, name: str, default: Any) -> Any:
    """读取配置字段，并兼容核心模块使用的字段别名。"""
    aliases = {
        "font_family": ("font_family", "font_name"),
        "offset_x": ("offset_x", "x_offset"),
        "offset_y": ("offset_y", "y_offset"),
    }.get(name, (name,))
    if isinstance(config, dict):
        for key in aliases:
            if key in config:
                return config[key]
        return default
    for key in aliases:
        if hasattr(config, key):
            return getattr(config, key)
    return default


def _set(config: Any, name: str, value: Any) -> None:
    """写入配置字段，并兼容 ``font_name``/``x_offset`` 等核心命名。"""
    aliases = {
        "font_family": ("font_family", "font_name"),
        "offset_x": ("offset_x", "x_offset"),
        "offset_y": ("offset_y", "y_offset"),
    }.get(name, (name,))
    if isinstance(config, dict):
        # 保留调用者字典已有的命名；新字典使用面板语义名称。
        key = next((candidate for candidate in aliases if candidate in config), aliases[0])
        config[key] = value
        return
    key = next((candidate for candidate in aliases if hasattr(config, candidate)), aliases[0])
    setattr(config, key, value)


def _connect_signal(obj: Any, names: tuple[str, ...], slot: Any) -> bool:
    """连接核心线程信号，兼容 ``progress`` 与 ``progress_signal`` 命名。"""

    for name in names:
        signal = getattr(obj, name, None)
        if signal is not None and hasattr(signal, "connect"):
            signal.connect(slot)
            return True
    return False


class SubtitleStyleDialog(QDialog):
    """字幕样式编辑及硬字幕导出对话框。

    ``video_path``、``subtitle_path`` 可留空；留空时用户在面板中选择。
    ``config`` 可以是核心 ``SubtitleConfig`` 实例，也可以是字典，面板
    的每个控件变化都会立即写回它。
    """

    export_requested = pyqtSignal(str, str, str, object)

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        video_path: str = "",
        subtitle_path: str = "",
        config: Any = None,
        ffmpeg_path: Optional[str] = None,
        playback_state: Any = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("字幕导出设置")
        self.setMinimumSize(620, 640)
        self.resize(700, 720)
        self.config = _new_config(config)
        # 语义更明确的别名，便于主窗口在 dialog.exec_() 后读取样式。
        self.subtitle_config = self.config
        self.worker = None
        self.ffmpeg_path = ffmpeg_path
        self.playback_state = playback_state
        self._default_output = ""
        self._state_timer = QTimer(self)
        self._state_timer.setInterval(300)
        self._state_timer.timeout.connect(self._refresh_playback_state)

        self._build_ui()
        self._load_config()
        self.video_edit.setText(video_path or "")
        self.subtitle_edit.setText(subtitle_path or "")
        self._update_default_output()
        self._refresh_playback_state()
        if self.playback_state is not None:
            self._state_timer.start()

    # ---- UI 构建 -------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        style_group = QGroupBox("字幕样式")
        form = QFormLayout(style_group)
        form.setLabelAlignment(Qt.AlignRight)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setVerticalSpacing(8)

        self.font_combo = QComboBox()
        self.font_combo.setEditable(True)
        self.font_combo.setInsertPolicy(QComboBox.NoInsert)
        families = list(QFontDatabase().families())
        # 常见中文字体优先显示；其余系统字体仍全部保留。
        preferred = [
            "Microsoft YaHei", "微软雅黑", "SimHei", "黑体", "SimSun", "宋体",
            "KaiTi", "楷体", "FZYaoti", "方正姚体", "Source Han Sans CN",
        ]
        ordered = [f for f in preferred if f in families]
        ordered.extend(f for f in sorted(families, key=str.casefold) if f not in ordered)
        self.font_combo.addItems(ordered)
        form.addRow("字体：", self.font_combo)

        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(8, 200)
        self.font_size_spin.setSuffix(" px")
        form.addRow("字号：", self.font_size_spin)

        self.primary_color = ColorButton()
        form.addRow("字体颜色：", self.primary_color)

        stroke_row = QWidget()
        stroke_layout = QHBoxLayout(stroke_row)
        stroke_layout.setContentsMargins(0, 0, 0, 0)
        self.outline_spin = QSpinBox()
        self.outline_spin.setRange(0, 20)
        self.outline_spin.setSuffix(" px")
        self.outline_color = ColorButton("#000000")
        stroke_layout.addWidget(self.outline_spin)
        stroke_layout.addSpacing(8)
        stroke_layout.addWidget(QLabel("颜色"))
        stroke_layout.addWidget(self.outline_color)
        form.addRow("描边：", stroke_row)

        background_row = QWidget()
        background_layout = QHBoxLayout(background_row)
        background_layout.setContentsMargins(0, 0, 0, 0)
        self.background_check = QCheckBox("启用底板")
        self.background_color = ColorButton("#000000")
        self.background_opacity = QSpinBox()
        self.background_opacity.setRange(0, 100)
        self.background_opacity.setSuffix(" %")
        background_layout.addWidget(self.background_check)
        background_layout.addWidget(self.background_color)
        background_layout.addWidget(QLabel("不透明度"))
        background_layout.addWidget(self.background_opacity)
        form.addRow("背景底色：", background_row)

        self.position_combo = QComboBox()
        self.position_combo.addItems(["顶部", "居中", "底部"])
        form.addRow("位置：", self.position_combo)

        offset_row = QWidget()
        offset_layout = QHBoxLayout(offset_row)
        offset_layout.setContentsMargins(0, 0, 0, 0)
        self.offset_x_spin = QSpinBox()
        self.offset_x_spin.setRange(-2000, 2000)
        self.offset_x_spin.setSuffix(" px")
        self.offset_y_spin = QSpinBox()
        self.offset_y_spin.setRange(-2000, 2000)
        self.offset_y_spin.setSuffix(" px")
        offset_layout.addWidget(QLabel("X"))
        offset_layout.addWidget(self.offset_x_spin)
        offset_layout.addSpacing(8)
        offset_layout.addWidget(QLabel("Y"))
        offset_layout.addWidget(self.offset_y_spin)
        form.addRow("位置微调：", offset_row)

        root.addWidget(style_group)

        state_group = QGroupBox("当前 mpv 播放状态（导出时自动读取最新快照）")
        state_layout = QVBoxLayout(state_group)
        self.state_label = QLabel("未连接 mpv")
        self.state_label.setWordWrap(True)
        state_layout.addWidget(self.state_label)
        root.addWidget(state_group)

        files_group = QGroupBox("输入与输出")
        files_form = QFormLayout(files_group)
        files_form.setLabelAlignment(Qt.AlignRight)
        self.video_edit, video_browse = self._path_row("选择视频", self._choose_video)
        self.subtitle_edit, subtitle_browse = self._path_row("选择 SRT", self._choose_subtitle)
        self.output_edit, output_browse = self._path_row("保存位置", self._choose_output)
        files_form.addRow("视频文件：", self._with_button(self.video_edit, video_browse))
        files_form.addRow("字幕文件：", self._with_button(self.subtitle_edit, subtitle_browse))
        files_form.addRow("输出文件：", self._with_button(self.output_edit, output_browse))
        root.addWidget(files_group)

        export_group = QGroupBox("导出")
        export_layout = QVBoxLayout(export_group)
        self.export_button = QPushButton("一键压制并导出视频")
        self.export_button.setMinimumHeight(36)
        self.export_button.setDefault(True)
        self.export_button.clicked.connect(self._start_export)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.status_label = QLabel("等待导出")
        self.status_label.setWordWrap(True)
        export_layout.addWidget(self.export_button)
        export_layout.addWidget(self.progress_bar)
        export_layout.addWidget(self.status_label)
        root.addWidget(export_group)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        # 所有样式控件都连接到同一个同步函数，保证 config 始终最新。
        self.font_combo.currentTextChanged.connect(self._sync_config)
        self.font_size_spin.valueChanged.connect(self._sync_config)
        self.outline_spin.valueChanged.connect(self._sync_config)
        self.background_check.toggled.connect(self._sync_config)
        self.background_opacity.valueChanged.connect(self._sync_config)
        self.position_combo.currentTextChanged.connect(self._sync_config)
        self.offset_x_spin.valueChanged.connect(self._sync_config)
        self.offset_y_spin.valueChanged.connect(self._sync_config)
        self.primary_color.colorChanged.connect(self._sync_config)
        self.outline_color.colorChanged.connect(self._sync_config)
        self.background_color.colorChanged.connect(self._sync_config)
        self.video_edit.textChanged.connect(self._update_default_output)
        self.background_check.toggled.connect(self._update_background_controls)
        self._style_controls = (
            self.font_combo,
            self.font_size_spin,
            self.primary_color,
            self.outline_spin,
            self.outline_color,
            self.background_check,
            self.background_color,
            self.background_opacity,
            self.position_combo,
            self.offset_x_spin,
            self.offset_y_spin,
        )

    @staticmethod
    def _with_button(edit: QLineEdit, button: QPushButton) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(edit)
        layout.addWidget(button)
        return container

    @staticmethod
    def _path_row(caption: str, callback: Any) -> tuple[QLineEdit, QPushButton]:
        edit = QLineEdit()
        edit.setPlaceholderText(caption)
        button = QPushButton("浏览…")
        button.clicked.connect(callback)
        return edit, button

    # ---- 配置读写 -----------------------------------------------------
    def _load_config(self) -> None:
        self.font_combo.setCurrentText(str(_get(self.config, "font_family", "Microsoft YaHei")))
        self.font_size_spin.setValue(int(_get(self.config, "font_size", 24)))
        self.primary_color.set_color(str(_get(self.config, "primary_color", "#FFFFFF")))
        self.outline_spin.setValue(int(_get(self.config, "outline_width", 2)))
        self.outline_color.set_color(str(_get(self.config, "outline_color", "#000000")))
        self.background_check.setChecked(bool(_get(self.config, "background_enabled", False)))
        self.background_color.set_color(str(_get(self.config, "background_color", "#000000")))
        # 核心 SubtitleConfig 使用 0~255 的 ASS 不透明度；UI 使用更
        # 直观的 0~100 百分比。字典配置则按 UI 百分比处理。
        opacity = int(_get(self.config, "background_opacity", 65))
        if not isinstance(self.config, dict) and opacity > 100:
            opacity = round(opacity * 100 / 255)
        self.background_opacity.setValue(max(0, min(100, opacity)))
        position = str(_get(self.config, "position", "bottom")).lower()
        position = {"top": "顶部", "center": "居中", "middle": "居中", "bottom": "底部"}.get(position, position)
        self.position_combo.setCurrentText(position if position in ("顶部", "居中", "底部") else "底部")
        self.offset_x_spin.setValue(int(_get(self.config, "offset_x", 0)))
        self.offset_y_spin.setValue(int(_get(self.config, "offset_y", 0)))
        self._update_background_controls()

    def _update_background_controls(self, enabled: Optional[bool] = None) -> None:
        """底板关闭时禁用底色与透明度控件，避免用户误以为它们生效。"""
        is_enabled = self.background_check.isChecked() if enabled is None else bool(enabled)
        self.background_color.setEnabled(is_enabled)
        self.background_opacity.setEnabled(is_enabled)

    def _sync_config(self, *_args: Any) -> None:
        _set(self.config, "font_family", self.font_combo.currentText())
        _set(self.config, "font_size", self.font_size_spin.value())
        _set(self.config, "primary_color", self.primary_color.color)
        _set(self.config, "outline_width", self.outline_spin.value())
        _set(self.config, "outline_color", self.outline_color.color)
        _set(self.config, "background_enabled", self.background_check.isChecked())
        _set(self.config, "background_color", self.background_color.color)
        opacity = self.background_opacity.value()
        # ASS/SubtitleConfig 约定的是不透明度 0~255；对普通字典保留百分比。
        if not isinstance(self.config, dict) and hasattr(self.config, "background_opacity"):
            opacity = round(opacity * 255 / 100)
        _set(self.config, "background_opacity", opacity)
        position = {"顶部": "top", "居中": "center", "底部": "bottom"}.get(
            self.position_combo.currentText(), self.position_combo.currentText()
        )
        _set(self.config, "position", position)
        _set(self.config, "offset_x", self.offset_x_spin.value())
        _set(self.config, "offset_y", self.offset_y_spin.value())

    # ---- 文件选择 -----------------------------------------------------
    def _choose_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择视频", "", "视频文件 (*.mp4 *.mkv *.mov *.avi *.webm *.flv);;所有文件 (*)"
        )
        if path:
            self.video_edit.setText(path)
            self._update_default_output()

    def _choose_subtitle(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择字幕", "", "SRT 字幕文件 (*.srt);;所有文件 (*)"
        )
        if path:
            self.subtitle_edit.setText(path)

    def _choose_output(self) -> None:
        default = self.output_edit.text() or self.video_edit.text()
        path, _ = QFileDialog.getSaveFileName(
            self, "导出视频", default, "MP4 视频 (*.mp4);;所有文件 (*)"
        )
        if path:
            self.output_edit.setText(path)

    def _update_default_output(self, *_args: Any) -> None:
        """视频路径变化时填充默认输出路径，但不覆盖用户手工修改。"""
        video = self.video_edit.text().strip()
        if not video:
            return
        old_default = self._default_output
        current = self.output_edit.text().strip()
        if derive_output_path is not None:
            try:
                new_default = str(derive_output_path(video))
            except Exception:
                new_default = str(Path(video).with_name(Path(video).stem + "-hard-sub.mp4"))
        else:
            new_default = str(Path(video).with_name(Path(video).stem + "-hard-sub.mp4"))
        if not current or current == old_default:
            self.output_edit.setText(new_default)
        self._default_output = new_default

    # ---- 导出线程 -----------------------------------------------------
    def _start_export(self) -> None:
        video = self.video_edit.text().strip()
        subtitle = self.subtitle_edit.text().strip()
        output = self.output_edit.text().strip()
        if not video or not os.path.isfile(video):
            QMessageBox.warning(self, "无法导出", "请选择存在的视频文件。")
            return
        if not subtitle or not os.path.isfile(subtitle):
            QMessageBox.warning(self, "无法导出", "请选择存在的 SRT 字幕文件。")
            return
        if Path(subtitle).suffix.lower() != ".srt":
            QMessageBox.warning(self, "无法导出", "当前硬字幕样式导出只支持 .srt 字幕文件。")
            return
        if not output:
            QMessageBox.warning(self, "无法导出", "请选择输出文件路径。")
            return
        if os.path.normcase(os.path.abspath(output)) == os.path.normcase(os.path.abspath(video)):
            QMessageBox.warning(self, "无法导出", "输出文件不能覆盖原视频。")
            return
        if os.path.exists(output):
            answer = QMessageBox.question(
                self, "覆盖文件？", f"目标文件已存在，是否覆盖？\n{output}",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        if HardSubtitleWorker is None:
            QMessageBox.critical(self, "组件缺失", "找不到 ffmpeg_worker.py 中的压制线程。")
            return

        self._sync_config()
        self.export_requested.emit(video, subtitle, output, self.config)
        self._set_export_running(True)
        self.progress_bar.setValue(0)
        self.status_label.setText("正在初始化…")
        try:
            kwargs = {"ffmpeg_path": self.ffmpeg_path} if self.ffmpeg_path else {}
            if self.playback_state is not None:
                kwargs["playback_state"] = self.playback_state
            self.worker = HardSubtitleWorker(video, subtitle, output, self.config, **kwargs)
        except TypeError:
            # 兼容核心实现未暴露 ffmpeg_path 参数的旧接口。
            self.worker = HardSubtitleWorker(video, subtitle, output, self.config)
        _connect_signal(self.worker, ("progress", "progress_signal"), self._on_progress)
        _connect_signal(self.worker, ("status", "status_signal", "message_signal"), self._on_status)
        _connect_signal(self.worker, ("finished", "finished_signal", "success"), self._on_finished)
        _connect_signal(self.worker, ("error", "error_signal", "failed"), self._on_error)
        self.worker.start()

    def _set_export_running(self, running: bool) -> None:
        self.export_button.setEnabled(not running)
        self.video_edit.setEnabled(not running)
        self.subtitle_edit.setEnabled(not running)
        self.output_edit.setEnabled(not running)
        for widget in getattr(self, "_style_controls", ()):
            widget.setEnabled(not running)
        if not running:
            self._update_background_controls()

    def _refresh_playback_state(self) -> None:
        """把 mpv IPC 的最新状态回显到导出确认面板。"""

        if self.playback_state is None:
            self.state_label.setText("未连接 mpv；将使用面板中的默认字幕参数。")
            return
        try:
            values = playback_state_values(self.playback_state) if playback_state_values else {}
            scale = float(values.get("sub_scale", 1.0) or 1.0)
            base = values.get("sub_font_size")
            base = float(base) if base is not None else float(_get(self.config, "font_size", 24))
            effective = max(1, round(base * scale))
            self.state_label.setText(
                "字号：{size}px（sub-scale={scale:.2f}）  |  字幕位置：{pos:.1f}%\n"
                "字幕延迟：{sub:.3f}s  |  音频延迟：{audio:.3f}s".format(
                    size=effective,
                    scale=scale,
                    pos=float(values.get("sub_pos", 100.0) or 100.0),
                    sub=float(values.get("sub_delay", 0.0) or 0.0),
                    audio=float(values.get("audio_delay", 0.0) or 0.0),
                )
            )
        except (TypeError, ValueError, AttributeError):
            self.state_label.setText("mpv 状态暂不可用，将使用当前面板参数。")

    def _on_progress(self, value: Any, *_args: Any) -> None:
        try:
            self.progress_bar.setValue(max(0, min(100, int(value))))
        except (TypeError, ValueError):
            pass

    def _on_status(self, message: Any, *_args: Any) -> None:
        self.status_label.setText(str(message))

    def _on_finished(self, path: Any, *_args: Any) -> None:
        self._set_export_running(False)
        self.progress_bar.setValue(100)
        self.status_label.setText(f"导出完成：{path}")
        QMessageBox.information(self, "导出完成", f"硬字幕视频已保存到：\n{path}")
        self.worker = None

    def _on_error(self, message: Any, *_args: Any) -> None:
        self._set_export_running(False)
        self.status_label.setText(f"导出失败：{message}")
        QMessageBox.critical(self, "导出失败", str(message))
        self.worker = None

    def closeEvent(self, event: Any) -> None:
        # 不强杀 FFmpeg，避免产生损坏的输出文件；导出期间提示用户。
        if self.worker is not None and self.worker.isRunning():
            answer = QMessageBox.question(
                self, "正在导出", "字幕仍在压制中，确定关闭面板吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            # 先让 worker 终止 FFmpeg，再销毁 QThread，避免 Qt 报
            # ``QThread: Destroyed while thread is still running``。
            stop = getattr(self.worker, "stop", None)
            if callable(stop):
                stop()
            if not self.worker.wait(3000):
                event.ignore()
                self.status_label.setText("正在停止字幕压制，请稍后再关闭…")
                return
        self._state_timer.stop()
        event.accept()


__all__ = ["ColorButton", "SubtitleStyleDialog"]
