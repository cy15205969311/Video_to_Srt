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
        self._process: Optional[subprocess.Popen] = None

    def stop(self) -> None:
        """请求取消压制。可由取消按钮或窗口关闭事件调用。"""

        self.requestInterruption()
        if self._process and self._process.poll() is None:
            self._process.terminate()

    def run(self) -> None:
        ass_temp: Optional[Path] = None
        try:
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
                # 与 force_style 相同的分辨率字号比例。
                if self.video_height is None:
                    self.video_height = probe_video_height(
                        input_video,
                        ffprobe_binary(self.ffmpeg_path),
                    )
                effective_config = config_for_playback_state(
                    self.config,
                    state,
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
            if self.export_config is not None:
                # build_ffmpeg_command 从同一 ExportConfig 取 input/output、样式
                # 和音频延迟，保证 force_style 与临时 ASS 完全一致。
                command = build_ffmpeg_command(
                    input_video,
                    ass_temp,
                    output_video,
                    ffmpeg_path=self.ffmpeg_path,
                    progress_pipe=True,
                    export_config=self.export_config,
                )
            else:
                command = build_ffmpeg_command(
                    input_video,
                    ass_temp,
                    output_video,
                    ffmpeg_path=self.ffmpeg_path,
                    progress_pipe=True,
                    # ASS 已由 effective_config 生成；这里只传同一份样式，确保
                    # force_style 与文件头完全一致。音频延迟由播放器快照提供。
                    subtitle_config=effective_config,
                    audio_delay=audio_delay,
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
