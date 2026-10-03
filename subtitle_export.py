"""富文本字幕导出与硬字幕压制。

本模块不依赖主窗口，负责以下工作：

* 保存字幕样式配置（:class:`SubtitleConfig`）；
* 将普通 SRT 转换为带样式的 ASS；
* 构建 Windows/Linux 均可用的 FFmpeg 硬字幕命令；
* 解析 FFmpeg 输出中的时间和进度；
* 由配套的 :mod:`ffmpeg_worker` 提供 PyQt5 压制线程，避免阻塞 GUI。

ASS 的颜色格式为 ``&HAABBGGRR&``，其中 AA 是透明度（00 表示完全不透明，
FF 表示完全透明），与网页常用的 ``#RRGGBB`` 不同，因此所有颜色转换都
集中在本文件内完成。
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence, Tuple, Union

from playback_state import PlaybackState, playback_state_values


ColorValue = Union[str, Tuple[int, int, int], Tuple[int, int, int, int], object]


@dataclass
class SubtitleConfig:
    """ASS 字幕样式配置。

    颜色可以使用 ``#RRGGBB``、``#AARRGGBB``、RGB/RGBA 元组，也可以传入
    PyQt 的 ``QColor``（只要对象提供 ``red()/green()/blue()/alpha()``）。
    ``background_opacity`` 表示底板不透明度，范围 0~255；ASS 内部会自动
    转为反向 alpha 值。

    ``position`` 支持 ``top``、``center``、``bottom``，也支持 ``left``、
    ``right`` 作为水平对齐提示（例如 ``top-left``、``bottom-right``）。
    ``x_offset``/``y_offset`` 为 ASS PlayRes 坐标中的偏移量。
    """

    font_name: str = "Microsoft YaHei"
    font_size: int = 24
    primary_color: ColorValue = "#FFFFFF"
    outline_width: int = 2
    outline_color: ColorValue = "#000000"
    background_enabled: bool = False
    background_color: ColorValue = "#000000"
    background_opacity: int = 160
    position: str = "bottom"
    x_offset: int = 0
    y_offset: int = 0
    # ASS 脚本坐标，FFmpeg 会按视频尺寸缩放。
    play_res_x: int = 1920
    play_res_y: int = 1080
    margin_horizontal: int = 40
    margin_vertical: int = 35
    encoding: str = "utf-8"
    _extra: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.font_size = max(1, int(self.font_size))
        self.outline_width = max(0, int(self.outline_width))
        self.background_opacity = max(0, min(255, int(self.background_opacity)))
        self.play_res_x = max(1, int(self.play_res_x))
        self.play_res_y = max(1, int(self.play_res_y))
        self.margin_horizontal = max(0, int(self.margin_horizontal))
        self.margin_vertical = max(0, int(self.margin_vertical))

    def copy(self, **changes: object) -> "SubtitleConfig":
        """返回一份可安全修改的配置副本。"""

        values = {
            key: getattr(self, key)
            for key in (
                "font_name",
                "font_size",
                "primary_color",
                "outline_width",
                "outline_color",
                "background_enabled",
                "background_color",
                "background_opacity",
                "position",
                "x_offset",
                "y_offset",
                "play_res_x",
                "play_res_y",
                "margin_horizontal",
                "margin_vertical",
                "encoding",
            )
        }
        values.update(changes)
        return SubtitleConfig(**values)


def _config_from_mapping(value: object) -> SubtitleConfig:
    """将 UI 常用的字典配置规范化为 ``SubtitleConfig``。"""

    if isinstance(value, SubtitleConfig):
        return value
    if not isinstance(value, dict):
        raise TypeError("字幕样式配置必须是 SubtitleConfig 或字典")

    def read(*names: str, default: object) -> object:
        for name in names:
            if name in value:
                return value[name]
        return default

    opacity = int(read("background_opacity", default=160))
    # 面板字典通常使用百分比；大于 100 时视为 ASS 的 0~255 数值。
    if 0 <= opacity <= 100:
        opacity = round(opacity * 255 / 100)
    position = str(read("position", default="bottom")).lower()
    position = {"顶部": "top", "居中": "center", "中部": "center", "底部": "bottom"}.get(position, position)
    return SubtitleConfig(
        font_name=str(read("font_name", "font_family", default="Microsoft YaHei")),
        font_size=int(read("font_size", default=24)),
        primary_color=read("primary_color", default="#FFFFFF"),
        outline_width=int(read("outline_width", default=2)),
        outline_color=read("outline_color", default="#000000"),
        background_enabled=bool(read("background_enabled", default=False)),
        background_color=read("background_color", default="#000000"),
        background_opacity=opacity,
        position=position,
        x_offset=int(read("x_offset", "offset_x", default=0)),
        y_offset=int(read("y_offset", "offset_y", default=0)),
    )


def sub_pos_to_margin_v(sub_pos: object, video_height: int = 1080) -> int:
    """把 mpv ``sub-pos`` 百分比换算成 ASS ``MarginV`` 像素。

    mpv 的 ``sub-pos`` 以画面顶部为 0、底部为 100；ASS 的 ``MarginV``
    则是从当前对齐边缘量起的像素距离。导出字幕默认使用底部对齐，
    因而需要反向换算：

    ``MarginV = round((100 - sub_pos) / 100 * video_height)``。

    例如 100（贴近底部）对应 0px，50 对应半个画面高度，0（顶部）
    对应一个画面高度。值会限制在 0~100%，防止 mpv/IPC 返回异常数字。
    ``video_height`` 使用 ASS PlayResY 时能保持与 ASS 脚本坐标一致。
    """

    try:
        percentage = float(sub_pos)
    except (TypeError, ValueError):
        percentage = 100.0
    if percentage != percentage:  # NaN
        percentage = 100.0
    percentage = max(0.0, min(100.0, percentage))
    try:
        height = max(1, int(video_height))
    except (TypeError, ValueError):
        height = 1080
    return int(round((100.0 - percentage) * height / 100.0))


def config_for_playback_state(
    config: Optional[Union[SubtitleConfig, dict]] = None,
    playback_state: object = None,
    *,
    video_height: Optional[int] = None,
) -> SubtitleConfig:
    """将播放器状态合并到字幕样式配置中。

    ``sub_scale`` 乘以基准字号（优先使用 ``sub_font_size``，否则使用
    ``SubtitleConfig.font_size``）；``sub_pos`` 按 :func:`sub_pos_to_margin_v`
    写入 ``margin_vertical``。函数返回副本，不修改 UI 保存的配置对象，
    适合在导出开始时对状态做一次快照。
    """

    base = _config_from_mapping(config or SubtitleConfig())
    # 未传播放器状态时，保留 UI 样式的字号/位置设置。不能把默认
    # ``sub_pos=100`` 当成状态覆盖掉 config.margin_vertical。
    if playback_state is None:
        return base
    values = playback_state_values(playback_state)
    try:
        scale = max(0.01, float(values.get("sub_scale", 1.0)))
    except (TypeError, ValueError):
        scale = 1.0
    raw_base_size = values.get("sub_font_size")
    try:
        base_size = float(raw_base_size) if raw_base_size is not None else float(base.font_size)
    except (TypeError, ValueError):
        base_size = float(base.font_size)
    base_size = max(1.0, base_size)
    height = video_height if video_height is not None else base.play_res_y
    # mpv 的 sub-pos 从画面顶部计数（0=顶部，100=底部），而 ASS 底部
    # 对齐样式的 MarginV 从底边计数，所以要反向线性换算：
    #     MarginV = round((100 - sub_pos) / 100 * PlayResY)
    # 这里使用 ASS PlayResY，而不是实际窗口像素，FFmpeg/libass 会按
    # 视频尺寸缩放；因此预览和成片在不同分辨率下仍保持同一相对位置。
    try:
        position = max(0.0, min(100.0, float(values.get("sub_pos", 100.0))))
    except (TypeError, ValueError):
        position = 100.0
    margin_v = int(round((100.0 - position) * max(1, int(height)) / 100.0))
    return base.copy(
        font_size=max(1, int(round(base_size * scale))),
        margin_vertical=margin_v,
    )


def _component(value: object, method: str, default: int) -> int:
    """安全读取 QColor 组件。"""

    try:
        result = getattr(value, method)
        result = result() if callable(result) else result
        return max(0, min(255, int(result)))
    except (AttributeError, TypeError, ValueError):
        return default


def color_to_rgba(value: ColorValue, *, default_alpha: int = 255) -> Tuple[int, int, int, int]:
    """将常见颜色表示转换成 ``(red, green, blue, alpha)``。

    字符串 ``#AARRGGBB`` 采用 Qt/CSS 透明度在前的约定；``#RRGGBBAA``
    若需要可直接传入 RGBA 元组，避免歧义。常见颜色名称也被支持。
    """

    if value is None:
        return 0, 0, 0, default_alpha

    # QColor 或其它兼容对象
    if not isinstance(value, (str, bytes, tuple, list)):
        if all(hasattr(value, attr) for attr in ("red", "green", "blue")):
            return (
                _component(value, "red", 0),
                _component(value, "green", 0),
                _component(value, "blue", 0),
                _component(value, "alpha", default_alpha),
            )

    if isinstance(value, (tuple, list)):
        numbers = list(value)
        if len(numbers) not in (3, 4):
            raise ValueError("颜色元组必须包含 RGB 或 RGBA 三/四个分量")
        try:
            channels = [max(0, min(255, int(channel))) for channel in numbers]
        except (TypeError, ValueError) as exc:
            raise ValueError("颜色元组包含无效分量") from exc
        if len(channels) == 3:
            channels.append(default_alpha)
        return tuple(channels)  # type: ignore[return-value]

    if isinstance(value, bytes):
        value = value.decode("ascii", errors="strict")
    text = str(value).strip()
    named = {
        "white": "#FFFFFF",
        "black": "#000000",
        "red": "#FF0000",
        "green": "#008000",
        "blue": "#0000FF",
        "yellow": "#FFFF00",
        "transparent": "#00000000",
    }
    text = named.get(text.lower(), text)
    if text.startswith("#"):
        text = text[1:]
    if text.lower().startswith("0x"):
        text = text[2:]
    if len(text) == 3:  # #RGB
        text = "".join(ch * 2 for ch in text)
        red, green, blue, alpha = int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16), default_alpha
        return red, green, blue, alpha
    if len(text) == 6:  # #RRGGBB
        try:
            return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16), default_alpha
        except ValueError as exc:
            raise ValueError(f"无法识别颜色值: {value!r}") from exc
    if len(text) != 8:
        raise ValueError(f"无法识别颜色值: {value!r}")
    try:
        # CSS/Qt 的八位写法是 AARRGGBB，本模块明确采用这个约定。
        alpha, red, green, blue = (
            int(text[0:2], 16),
            int(text[2:4], 16),
            int(text[4:6], 16),
            int(text[6:8], 16),
        )
    except ValueError as exc:
        raise ValueError(f"无法识别颜色值: {value!r}") from exc
    return red, green, blue, alpha


def ass_color(value: ColorValue, *, default_alpha: int = 255) -> str:
    """将颜色转换为 ASS 的 ``&HAABBGGRR&`` 字符串。"""

    red, green, blue, alpha = color_to_rgba(value, default_alpha=default_alpha)
    # ASS alpha 的含义与 CSS alpha 相反：00 不透明，FF 完全透明。
    ass_alpha = 255 - alpha
    return f"&H{ass_alpha:02X}{blue:02X}{green:02X}{red:02X}&"


def _ass_time(seconds: float) -> str:
    """将秒数转换为 ASS 的 h:mm:ss.cc 格式。"""

    total_centiseconds = max(0, int(round(float(seconds) * 100)))
    hours, remainder = divmod(total_centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    secs, centiseconds = divmod(remainder, 100)
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{centiseconds:02d}"


_SRT_TIME = re.compile(
    r"(?P<h>\d{1,3}):(?P<m>\d{2}):(?P<s>\d{2})(?:[,.](?P<ms>\d{1,3}))?"
)


def _parse_srt_time(value: str) -> float:
    match = _SRT_TIME.search(value.strip())
    if not match:
        raise ValueError(f"无效的 SRT 时间: {value!r}")
    milliseconds = (match.group("ms") or "0").ljust(3, "0")[:3]
    return (
        int(match.group("h")) * 3600
        + int(match.group("m")) * 60
        + int(match.group("s"))
        + int(milliseconds) / 1000
    )


def _ass_text(lines: Iterable[str]) -> str:
    """处理 SRT 多行文本并转为 ASS 的换行标记。"""

    escaped_lines = []
    for line in lines:
        # 花括号会被 ASS 当作覆盖标签，反斜杠会触发 ``\N`` 等控制码；
        # SRT 普通文本需要先转义，避免字幕内容破坏样式。
        escaped_lines.append(
            line.rstrip("\r").replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
        )
    text = r"\N".join(escaped_lines)
    # Dialogue 字段以逗号分隔，ASS 文本中的逗号合法；清理可能导致样式
    # 误解析的 BOM，并保留用户输入的其它文字。
    return text.replace("\ufeff", "").replace("\r", "")


def _position_alignment(position: str) -> int:
    value = (position or "bottom").strip().lower().replace(" ", "-")
    horizontal = "center"
    vertical = "bottom"
    if "left" in value:
        horizontal = "left"
    elif "right" in value:
        horizontal = "right"
    if "top" in value:
        vertical = "top"
    elif "center" in value or "middle" in value:
        vertical = "middle"
    # ASS alignment: 1/2/3 bottom, 4/5/6 middle, 7/8/9 top。
    horizontal_index = {"left": 1, "center": 2, "right": 3}[horizontal]
    return {"bottom": horizontal_index, "middle": horizontal_index + 3, "top": horizontal_index + 6}[vertical]


def _position_override(config: SubtitleConfig) -> str:
    r"""当配置有偏移量时，用 ASS ``\pos`` 覆盖默认样式位置。"""

    if not config.x_offset and not config.y_offset:
        return ""
    alignment = _position_alignment(config.position)
    horizontal_index = (alignment - 1) % 3
    vertical_index = (alignment - 1) // 3
    x = {0: config.margin_horizontal, 1: config.play_res_x // 2, 2: config.play_res_x - config.margin_horizontal}[horizontal_index]
    y = {0: config.play_res_y - config.margin_vertical, 1: config.play_res_y // 2, 2: config.margin_vertical}[vertical_index]
    return rf"{{\pos({x + int(config.x_offset)},{y + int(config.y_offset)})}}"


def _read_text(path: Union[str, os.PathLike[str]], encoding: str = "utf-8") -> str:
    path_obj = Path(path)
    try:
        return path_obj.read_text(encoding=encoding)
    except UnicodeDecodeError:
        # Windows 上历史字幕经常是 GB18030，UTF-8 失败时自动回退。
        return path_obj.read_text(encoding="gb18030")


def srt_to_ass_text(
    srt_text: str,
    config: Optional[SubtitleConfig] = None,
    *,
    subtitle_delay: float = 0.0,
) -> str:
    """将 SRT 文本转换成 ASS 文本，并可平移字幕时间轴。

    ``subtitle_delay`` 单位为秒，正数让字幕整体向后延迟，负数让字幕
    提前。事件完全落在 0 秒以前时会被丢弃，跨过 0 秒的事件从 0 秒开始，
    这样生成的 ASS 时间始终符合 FFmpeg 的非负时间要求。
    """

    config = _config_from_mapping(config or SubtitleConfig())
    primary = ass_color(config.primary_color)
    outline = ass_color(config.outline_color)
    back_alpha = config.background_opacity if config.background_enabled else 0
    back = ass_color(config.background_color, default_alpha=back_alpha)
    border_style = 3 if config.background_enabled else 1
    alignment = _position_alignment(config.position)
    # ASS 样式字段顺序固定，避免不同 FFmpeg 版本解析失败。
    header = f"""[Script Info]
; Generated by Video_to_Srt
ScriptType: v4.00+
PlayResX: {config.play_res_x}
PlayResY: {config.play_res_y}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{config.font_name},{config.font_size},{primary},{primary},{outline},{back},0,0,0,0,100,100,0,0,{border_style},{config.outline_width},0,{alignment},{config.margin_horizontal},{config.margin_horizontal},{config.margin_vertical},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    blocks = re.split(r"\r?\n\s*\r?\n", srt_text.replace("\ufeff", "").strip())
    dialogues = []
    for block in blocks:
        lines = block.splitlines()
        if not lines:
            continue
        # 编号行可缺失，只要第二行是时间轴即可。
        timing_index = next((index for index, line in enumerate(lines[:3]) if "-->" in line), None)
        if timing_index is None:
            continue
        timing = lines[timing_index].split("-->", 1)
        if len(timing) != 2:
            continue
        try:
            start = _parse_srt_time(timing[0])
            # SRT 结束时间可能带有定位/样式信息，只读取最前面的时间。
            end = _parse_srt_time(timing[1])
        except ValueError:
            continue
        try:
            shift = float(subtitle_delay)
        except (TypeError, ValueError):
            shift = 0.0
        start += shift
        end += shift
        if end <= 0:
            continue
        start = max(0.0, start)
        text_lines = lines[timing_index + 1 :]
        text = _ass_text(text_lines)
        if not text:
            continue
        dialogues.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{_position_override(config)}{text}"
        )
    return header + "\n".join(dialogues) + ("\n" if dialogues else "")


def convert_srt_to_ass(
    srt_path: Union[str, os.PathLike[str]],
    ass_path: Optional[Union[str, os.PathLike[str]]] = None,
    config: Optional[SubtitleConfig] = None,
    *,
    subtitle_delay: float = 0.0,
) -> str:
    """读取 SRT 并写入 ASS，返回实际输出路径。"""

    srt_path = Path(srt_path)
    if not srt_path.is_file():
        raise FileNotFoundError(f"字幕文件不存在: {srt_path}")
    if ass_path is None:
        ass_path = srt_path.with_suffix(".ass")
    ass_path = Path(ass_path)
    ass_path.parent.mkdir(parents=True, exist_ok=True)
    current_config = config or SubtitleConfig()
    ass_path.write_text(
        srt_to_ass_text(
            _read_text(srt_path, current_config.encoding),
            current_config,
            subtitle_delay=subtitle_delay,
        ),
        encoding="utf-8-sig",
    )
    return str(ass_path)


def escape_subtitles_filter_path(path: Union[str, os.PathLike[str]]) -> str:
    """转义 FFmpeg subtitles 滤镜里的文件路径。

    FFmpeg 滤镜表达式仍会解析 Windows 盘符的冒号，即便 subprocess 使用
    参数列表而不是 shell，因此这里必须将 ``C:\\`` 规范成 ``C\\:/``。
    """

    value = str(Path(path).resolve()).replace("\\", "/")
    value = value.replace("\\", r"\\").replace(":", r"\:")
    value = value.replace("'", r"\'")
    return value


def force_style_from_config(config: Optional[Union[SubtitleConfig, dict]] = None) -> str:
    """生成 FFmpeg ``subtitles`` 滤镜的 ``force_style`` 值。

    ASS 文件本身已经保存了完整样式；这里再次显式传入关键字段，是为了
    确保 FFmpeg 渲染时采用播放器状态快照，而不是播放器后来又被用户调整
    过的旧文件或默认样式。返回值不含外围单引号，调用方可安全组合滤镜。
    """

    current = _config_from_mapping(config or SubtitleConfig())
    primary = ass_color(current.primary_color)
    outline = ass_color(current.outline_color)
    back_alpha = current.background_opacity if current.background_enabled else 0
    back = ass_color(current.background_color, default_alpha=back_alpha)
    values = {
        "Fontname": str(current.font_name),
        "Fontsize": str(max(1, int(current.font_size))),
        "PrimaryColour": primary,
        "OutlineColour": outline,
        "BackColour": back,
        "BorderStyle": "3" if current.background_enabled else "1",
        "Outline": str(max(0, int(current.outline_width))),
        "Alignment": str(_position_alignment(current.position)),
        "MarginL": str(max(0, int(current.margin_horizontal))),
        "MarginR": str(max(0, int(current.margin_horizontal))),
        "MarginV": str(max(0, int(current.margin_vertical))),
    }

    def escape(value: str) -> str:
        # 逗号是 force_style 的字段分隔符，需反斜杠转义；单引号则由
        # 外围滤镜引号包裹，避免用户字体名导致滤镜截断。
        return value.replace("\\", r"\\").replace(",", r"\,").replace("'", r"\'")

    return ",".join(f"{key}={escape(value)}" for key, value in values.items())


def build_hardsub_command(
    input_video: Union[str, os.PathLike[str]],
    ass_path: Union[str, os.PathLike[str]],
    output_video: Union[str, os.PathLike[str]],
    *,
    ffmpeg_path: Union[str, os.PathLike[str]] = "ffmpeg",
    overwrite: bool = True,
    video_codec: str = "libx264",
    crf: int = 18,
    preset: str = "medium",
    copy_audio: bool = True,
    progress_pipe: bool = False,
    subtitle_config: Optional[Union[SubtitleConfig, dict]] = None,
    playback_state: object = None,
    video_height: Optional[int] = None,
    audio_delay: Optional[float] = None,
) -> list[str]:
    """构建 ``subprocess.Popen`` 可直接使用的 FFmpeg 参数列表。

    ``playback_state`` 可以是 :class:`PlaybackState`、字典或任意具有同名
    属性的对象。传入后会将字号/位置写入 ``force_style``，并把音频延迟
    映射成 ``adelay``（正数）或 ``atrim``（负数）。为了兼容旧调用，所有
    新参数都是可选的。
    """

    input_video = str(input_video)
    output_video = str(output_video)
    effective_config = config_for_playback_state(subtitle_config, playback_state, video_height=video_height)
    style = force_style_from_config(effective_config)
    ass_filter = f"subtitles='{escape_subtitles_filter_path(ass_path)}':force_style='{style}'"
    command = [str(ffmpeg_path), "-hide_banner"]
    command.append("-y" if overwrite else "-n")
    command.extend(["-i", input_video, "-vf", ass_filter])

    state_values = playback_state_values(playback_state)
    if audio_delay is None:
        audio_delay = state_values.get("audio_delay", 0.0)
    try:
        delay = float(audio_delay or 0.0)
    except (TypeError, ValueError):
        delay = 0.0
    # copy_audio 无法对时间轴做修改；有延迟时必须解码后再编码音频。
    if abs(delay) > 1e-9:
        if delay > 0:
            milliseconds = max(1, int(round(delay * 1000)))
            audio_filter = f"adelay={milliseconds}:all=1"
        else:
            # adelay 不支持负数；丢弃开头 |delay| 秒并重置 PTS，即可让音频
            # 相对视频提前播放。视频流仍从 0 秒开始，字幕时间轴独立处理。
            seconds = max(0.0, -delay)
            audio_filter = f"atrim=start={seconds:.6f},asetpts=PTS-STARTPTS"
        command.extend([
            "-filter_complex",
            f"[0:a]{audio_filter}[aout]",
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
            "-c:a",
            "aac",
        ])
    else:
        command.extend(["-c:a", "copy" if copy_audio else "aac"])
    command.extend(["-c:v", video_codec, "-crf", str(max(0, int(crf))), "-preset", preset])
    if progress_pipe:
        # pipe:2 与普通日志共用 stderr；-nostats 让 worker 只需解析进度键值。
        command.extend(["-progress", "pipe:2", "-nostats"])
    command.append(output_video)
    return command


def ffmpeg_binary(default: Optional[Union[str, os.PathLike[str]]] = None) -> str:
    """查找项目内打包的 FFmpeg，找不到时回退到 PATH 中的 ffmpeg。"""

    if default:
        return str(default)
    root = Path(__file__).resolve().parent
    names = ("ffmpeg.exe", "ffmpeg") if os.name == "nt" else ("ffmpeg", "ffmpeg.exe")
    for name in names:
        candidate = root / "ffmpeg-6.0-essentials_build" / "bin" / name
        if candidate.is_file():
            return str(candidate)
    return "ffmpeg.exe" if os.name == "nt" else "ffmpeg"


def ffprobe_binary(ffmpeg_path: Optional[Union[str, os.PathLike[str]]] = None) -> str:
    """根据 ffmpeg 路径推导 ffprobe 路径。"""

    resolved = Path(ffmpeg_path or ffmpeg_binary())
    probe_name = "ffprobe.exe" if resolved.suffix.lower() == ".exe" else "ffprobe"
    sibling = resolved.with_name(probe_name)
    return str(sibling) if sibling.exists() else probe_name


def probe_duration(video_path: Union[str, os.PathLike[str]], ffprobe_path: Optional[Union[str, os.PathLike[str]]] = None) -> Optional[float]:
    """通过 ffprobe 获取视频时长，失败时返回 None。"""

    command = [str(ffprobe_path or ffprobe_binary()), "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)]
    try:
        completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30, check=False)
        value = float(completed.stdout.strip())
        return value if value > 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


_FFMPEG_TIME = re.compile(r"(?:^|\s)time=(?P<time>\d{1,3}:\d{2}:\d{2}(?:[.:]\d+)?)")
_FFMPEG_OUT_TIME = re.compile(r"(?:^|\s)out_time_(?:ms|us)=(?P<value>-?\d+)")
_FFMPEG_FRAME = re.compile(r"(?:^|\s)frame=(?P<frame>\d+)")
_FFMPEG_DURATION = re.compile(r"Duration:\s*(?P<time>\d{1,3}:\d{2}:\d{2}(?:[.:]\d+)?)")


def parse_ffmpeg_time(value: str) -> Optional[float]:
    """解析 ``HH:MM:SS.xx``，兼容 FFmpeg 使用冒号或点分隔小数。"""

    value = value.strip().replace(",", ".")
    match = re.fullmatch(r"(\d+):(\d{2}):(\d{2})(?:[.:](\d+))?", value)
    if not match:
        return None
    fraction = match.group(4) or "0"
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3)) + float(f"0.{fraction}")


def parse_ffmpeg_progress(
    line: str,
    duration_seconds: Optional[float],
    total_frames: Optional[int] = None,
) -> Optional[int]:
    """从 FFmpeg 一行输出解析 0~100 的进度。

    常规情况下按 ``time=``/``out_time_ms=`` 和媒体时长计算；调用方如果
    已知总帧数，也可以传入 ``total_frames``，此时没有时间字段时会回退到
    ``frame=`` 进度。
    """

    match = _FFMPEG_TIME.search(line)
    current: Optional[float] = None
    if match:
        current = parse_ffmpeg_time(match.group("time"))
    else:
        out_match = _FFMPEG_OUT_TIME.search(line)
        if out_match:
            # out_time_ms 和 out_time_us 都按微秒输出（即使字段名带 ms）。
            raw = int(out_match.group("value"))
            current = raw / 1_000_000
    if current is not None and duration_seconds and duration_seconds > 0:
        return max(0, min(100, int(round(current / duration_seconds * 100))))
    if total_frames and total_frames > 0:
        frame_match = _FFMPEG_FRAME.search(line)
        if frame_match:
            return max(0, min(100, int(round(int(frame_match.group("frame")) / total_frames * 100))))
    return None


def parse_ffmpeg_duration(line: str) -> Optional[float]:
    match = _FFMPEG_DURATION.search(line)
    return parse_ffmpeg_time(match.group("time")) if match else None


# 面向主界面的短名称 API。保留上面的显式名称，便于调用方按语义选择；
# 这些别名也让旧版本集成代码可以平滑迁移。
def srt_to_ass(
    srt_text: str,
    config: Optional[SubtitleConfig] = None,
    *,
    subtitle_delay: float = 0.0,
) -> str:
    """``srt_to_ass_text`` 的简洁别名。"""

    return srt_to_ass_text(srt_text, config, subtitle_delay=subtitle_delay)


def write_ass_from_srt(
    srt_path: Union[str, os.PathLike[str]],
    ass_path: Optional[Union[str, os.PathLike[str]]] = None,
    config: Optional[SubtitleConfig] = None,
    *,
    subtitle_delay: float = 0.0,
) -> str:
    """读取 SRT 并写 ASS，返回 ASS 路径。"""

    return convert_srt_to_ass(srt_path, ass_path, config, subtitle_delay=subtitle_delay)


def build_ffmpeg_command(*args, **kwargs) -> list[str]:
    """``build_hardsub_command`` 的兼容别名。"""

    return build_hardsub_command(*args, **kwargs)


def parse_progress_line(
    line: str,
    duration_seconds: Optional[float],
    total_frames: Optional[int] = None,
) -> Optional[int]:
    """解析 FFmpeg 进度行；名称更适合直接连接 UI 进度回调。"""

    return parse_ffmpeg_progress(line, duration_seconds, total_frames)


def probe_media_duration(
    video_path: Union[str, os.PathLike[str]],
    ffprobe_path: Optional[Union[str, os.PathLike[str]]] = None,
) -> Optional[float]:
    """``probe_duration`` 的兼容别名。"""

    return probe_duration(video_path, ffprobe_path)


def derive_output_path(
    input_video: Union[str, os.PathLike[str]],
    suffix: str = "-hard-sub",
) -> str:
    """按输入视频文件名生成默认硬字幕输出路径。"""

    source = Path(input_video)
    clean_suffix = str(suffix or "-hard-sub")
    if not clean_suffix.startswith(".") and not clean_suffix.startswith("-"):
        clean_suffix = "-" + clean_suffix
    return str(source.with_name(source.stem + clean_suffix + source.suffix))


__all__ = [
    "PlaybackState",
    "SubtitleConfig",
    "config_for_playback_state",
    "ass_color",
    "build_ffmpeg_command",
    "build_hardsub_command",
    "color_to_rgba",
    "convert_srt_to_ass",
    "derive_output_path",
    "escape_subtitles_filter_path",
    "force_style_from_config",
    "ffmpeg_binary",
    "parse_ffmpeg_duration",
    "parse_ffmpeg_progress",
    "parse_ffmpeg_time",
    "parse_progress_line",
    "probe_duration",
    "probe_media_duration",
    "srt_to_ass",
    "srt_to_ass_text",
    "sub_pos_to_margin_v",
    "write_ass_from_srt",
]
