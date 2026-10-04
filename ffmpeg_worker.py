"""PyQt5 后台硬字幕压制线程。

业务转换函数位于 :mod:`subtitle_export`，本文件只负责线程和 Qt 信号，
这样在命令行测试字幕转换时不需要初始化 QApplication。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from PyQt5.QtCore import QThread, pyqtSignal

from subtitle_export import (
    ExportConfig,
    PlaybackState,
    SubtitleConfig,
    build_ffmpeg_command,
    config_for_playback_state,
    convert_srt_to_ass,
    ffmpeg_binary,
    ffprobe_binary,
    parse_ffmpeg_duration,
    parse_progress_line,
    playback_state_values,
    probe_media_duration,
    probe_video_height,
)

# ``probe_video_dimensions`` is added by newer versions of subtitle_export.
# Keep the worker importable with an older module while the application is
# upgraded (the height-only probe remains a safe fallback).
try:
    from subtitle_export import probe_video_dimensions
except ImportError:  # pragma: no cover - compatibility with old deployments
    probe_video_dimensions = None  # type: ignore[assignment]


def _probe_dimensions(video_path: str, ffmpeg_path: str) -> tuple[Optional[int], Optional[int]]:
    """读取视频宽高，兼容不同版本的 probe_video_dimensions 返回值。"""

    if probe_video_dimensions is not None:
        try:
            value = probe_video_dimensions(video_path, ffprobe_binary(ffmpeg_path))
            if isinstance(value, dict):
                width = value.get("width")
                height = value.get("height")
            elif isinstance(value, (tuple, list)) and len(value) >= 2:
                width, height = value[0], value[1]
            elif isinstance(value, (int, float)):
                # Older probe helpers returned only the height. Preserve that
                # fallback so a legacy ffprobe wrapper still gets a usable
                # ASS PlayResY.
                width, height = None, value
            else:
                width = height = None
            width = int(width) if width is not None and int(width) > 0 else None
            height = int(height) if height is not None and int(height) > 0 else None
            return width, height
        except (OSError, TypeError, ValueError, AttributeError):
            pass
    # Older subtitle_export only exposed the height probe.  Returning a
    # missing width allows the ASS generator to retain its configured width.
    try:
        height = probe_video_height(video_path, ffprobe_binary(ffmpeg_path))
    except (OSError, TypeError, ValueError):
        height = None
    return None, height


class HardSubtitleWorker(QThread):
    """在独立 QThread 中将样式字幕烧录到视频。

    ``progress`` 发出 0~100 整数；``status`` 发出给用户看的中文提示；
    ``finished`` 发出输出视频路径；``error`` 发出异常文本。
    """

    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    finished = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(
        self,
        input_video: str,
        srt_path: str,
        output_video: str,
        config: Optional[SubtitleConfig] = None,
        *,
        ffmpeg_path: Optional[str] = None,
        playback_state: object = None,
        export_config: Optional[ExportConfig] = None,
        video_height: Optional[int] = None,
        video_width: Optional[int] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        # ``ExportConfig`` 是在 GUI 线程中合并并冻结的单一真相。允许
        # 调用方通过关键字传入，也允许把它误传到第四个位置参数（兼容
        # 主界面旧集成方式），但后续线程绝不再读取活动中的控件或 mpv。
        if export_config is None and isinstance(config, ExportConfig):
            export_config = config
        self.export_config = export_config
        if export_config is not None:
            self.input_video = export_config.input_video
            self.srt_path = export_config.subtitle_path
            self.output_video = export_config.output_video
            self.config = export_config.subtitle_config
            # 冻结快照已经包含所有状态；保留引用仅用于旧接口兼容，run()
            # 会优先消费 export_config，避免 sub_scale 被重复乘一次。
            self.playback_state = None
        else:
            self.input_video = input_video
            self.srt_path = srt_path
            self.output_video = output_video
            self.config = config or SubtitleConfig()
            self.playback_state = playback_state
        self.ffmpeg_path = ffmpeg_path or ffmpeg_binary()
        self.video_height = video_height
        self.video_width = video_width
        self._process: Optional[subprocess.Popen] = None

    def stop(self) -> None:
        """请求取消压制。可由取消按钮或窗口关闭事件调用。"""

        self.requestInterruption()
        if self._process and self._process.poll() is None:
            self._process.terminate()

    def run(self) -> None:
        ass_temp: Optional[Path] = None
        try:
            # ASS 的 PlayRes 必须与实际源视频画布一致。即使 GUI 已经创建
            # ExportConfig，也在这里做一次只读探测，确保 mpv 触发导出时
            # 不会继续沿用默认的 1920x1080 坐标。
            probed_width, probed_height = _probe_dimensions(
                self.input_video,
                self.ffmpeg_path,
            )
            if self.video_width is None:
                self.video_width = probed_width
            if self.video_height is None:
                self.video_height = probed_height
            if self.export_config is not None:
                # 这份对象已在 _start_export 中从 GUI 控件和 mpv IPC
                # 快照合并完成。不能再调用 config_for_playback_state，
                # 否则会把 sub_scale 二次乘到字号上。
                export = self.export_config
                effective_config = export.subtitle_config
                subtitle_delay = float(export.sub_delay)
                audio_delay = float(export.audio_delay)
                input_video = export.input_video
                srt_path = export.subtitle_path
                output_video = export.output_video
            else:
                # 旧调用方式仍支持动态 PlaybackState；在进入线程时复制
                # 一次快照，避免压制中 mpv 菜单的变化造成样式前后不一致。
                state = self.playback_state
                snapshot = getattr(state, "snapshot", None)
                if callable(snapshot):
                    state = snapshot()
                state_values = playback_state_values(state)
                input_video = self.input_video
                srt_path = self.srt_path
                output_video = self.output_video
                # 旧接口没有在 GUI 线程创建 ExportConfig 时，在进入
                # 压制前补探测一次真实视频高度。这样 ASS 文件也会应用
                # 与标准 ASS 文件相同的分辨率字号比例。
                if self.video_width is None or self.video_height is None:
                    width, height = _probe_dimensions(input_video, self.ffmpeg_path)
                    if self.video_width is None:
                        self.video_width = width
                    if self.video_height is None:
                        self.video_height = height
                effective_config = config_for_playback_state(
                    self.config,
                    state,
                    video_width=self.video_width,
                    video_height=self.video_height,
                )
                try:
                    subtitle_delay = float(state_values.get("sub_delay", 0.0) or 0.0)
                except (TypeError, ValueError):
                    subtitle_delay = 0.0
                try:
                    audio_delay = float(state_values.get("audio_delay", 0.0) or 0.0)
                except (TypeError, ValueError):
                    audio_delay = 0.0

            # 生成的 ASS 文件头使用源视频真实分辨率。这里仅更新坐标系，
            # 不再重新计算字号/描边；这些指标已经在 ExportConfig 快照中
            # 按 mpv 状态和分辨率基准完成，避免二次缩放。
            dimensions = {}
            if self.video_width and self.video_width > 0:
                dimensions["play_res_x"] = int(self.video_width)
            if self.video_height and self.video_height > 0:
                dimensions["play_res_y"] = int(self.video_height)
            if dimensions and hasattr(effective_config, "copy"):
                effective_config = effective_config.copy(**dimensions)
            self.status.emit("正在生成 ASS 字幕…")
            # NamedTemporaryFile 在 Windows 上先关闭句柄，否则 FFmpeg 无法读取。
            with tempfile.NamedTemporaryFile(prefix="video_to_srt_", suffix=".ass", delete=False) as handle:
                ass_temp = Path(handle.name)
            convert_srt_to_ass(
                srt_path,
                ass_temp,
                effective_config,
                subtitle_delay=subtitle_delay,
            )

            duration = probe_media_duration(input_video, ffprobe_binary(self.ffmpeg_path))
            # 将临时 ASS 路径也放入导出快照；新版 build_hardsub_command
            # 会据此选择 ``-vf ass=...``，而不是再次套用 SRT 样式覆盖。
            command_export = ExportConfig(
                input_video=input_video,
                subtitle_path=str(ass_temp),
                output_video=output_video,
                subtitle_config=effective_config,
                sub_scale=1.0,
                sub_pos=100.0,
                sub_delay=subtitle_delay,
                audio_delay=audio_delay,
            )
            command = build_ffmpeg_command(
                input_video,
                ass_temp,
                output_video,
                ffmpeg_path=self.ffmpeg_path,
                progress_pipe=True,
                export_config=command_export,
            )
            Path(output_video).resolve().parent.mkdir(parents=True, exist_ok=True)
            self.status.emit("正在渲染字幕…")
            self.progress.emit(0)
            diagnostics = []
            self._process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                universal_newlines=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            assert self._process.stderr is not None
            for line in self._process.stderr:
                if self.isInterruptionRequested():
                    self._process.terminate()
                    raise RuntimeError("用户取消了字幕压制")
                if duration is None:
                    discovered = parse_ffmpeg_duration(line)
                    if discovered:
                        duration = discovered
                value = parse_progress_line(line, duration)
                if value is not None:
                    self.progress.emit(value)
                    self.status.emit(f"正在渲染字幕 ({value}%)…")
                elif line.strip():
                    diagnostics.append(line.strip())
                    del diagnostics[:-8]
            return_code = self._process.wait()
            if return_code != 0:
                detail = "\n".join(diagnostics[-3:])
                suffix = f"：{detail}" if detail else ""
                raise RuntimeError(f"FFmpeg 压制失败（退出码 {return_code}）{suffix}")
            self.progress.emit(100)
            self.status.emit("字幕压制完成")
            self.finished.emit(output_video)
        except Exception as exc:  # Qt 线程错误通过信号传递，避免 GUI 崩溃。
            self.error.emit(str(exc))
        finally:
            self._process = None
            if ass_temp:
                try:
                    ass_temp.unlink(missing_ok=True)
                except OSError:
                    pass


__all__ = ["HardSubtitleWorker"]
