"""mpv 播放状态与 JSON IPC 同步。

主窗口目前以 ``subprocess`` 启动便携版 mpv；这种方式不会提供
``python-mpv`` 的 ``observe_property`` API。本模块通过 mpv 的
``--input-ipc-server`` JSON 行协议订阅同一组属性，把右键 OSD/快捷键修改的
值实时写入 :class:`PlaybackState`。模块不依赖 PyQt，因此可以在 GUI 线程和
命令行测试中复用。

Windows 上 mpv 的 IPC 端点通常是 ``\\\\.\\pipe\\name`` 命名管道，Unix 上
则是 Unix domain socket。``MpvJsonIpc`` 同时支持这两种端点以及
``tcp://host:port``（方便测试或自定义 mpv 构建）。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple, Union


PropertyListener = Callable[[str, Any, "PlaybackState"], None]


@dataclass
class PlaybackState:
    """播放器当前的可导出状态。

    ``sub_pos`` 遵循 mpv 约定，范围是 0~100（0 为顶部，100 为底部）。
    ``sub_font_size`` 是可选的绝对字号；mpv 的 ``sub-scale`` 只表示比例，
    导出端可用 ``base_font_size * sub_scale`` 计算字号。
    """

    sub_delay: float = 0.0
    audio_delay: float = 0.0
    sub_scale: float = 1.0
    sub_font_size: Optional[float] = None
    sub_pos: float = 100.0
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False, compare=False)
    _listeners: list[PropertyListener] = field(default_factory=list, init=False, repr=False, compare=False)

    _PROPERTY_MAP = {
        "sub-delay": "sub_delay",
        "audio-delay": "audio_delay",
        "sub-scale": "sub_scale",
        "sub-font-size": "sub_font_size",
        "sub-pos": "sub_pos",
    }

    def __post_init__(self) -> None:
        # 通过构造函数恢复状态时也执行同样的范围约束。
        self.sub_delay = self._number(self.sub_delay, 0.0)
        self.audio_delay = self._number(self.audio_delay, 0.0)
        self.sub_scale = max(0.01, self._number(self.sub_scale, 1.0))
        self.sub_font_size = self._optional_number(self.sub_font_size)
        self.sub_pos = max(0.0, min(100.0, self._number(self.sub_pos, 100.0)))

    @staticmethod
    def _number(value: Any, default: float) -> float:
        try:
            result = float(value)
            return result if result == result and abs(result) != float("inf") else default
        except (TypeError, ValueError):
            return default

    @classmethod
    def _optional_number(cls, value: Any) -> Optional[float]:
        if value is None:
            return None
        return cls._number(value, 0.0)

    def add_listener(self, callback: PropertyListener) -> None:
        """注册状态变化回调；重复注册会被忽略。"""

        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_listener(self, callback: PropertyListener) -> None:
        with self._lock:
            try:
                self._listeners.remove(callback)
            except ValueError:
                pass

    def update_property(self, name: str, value: Any) -> bool:
        """把 mpv 属性名和值写入状态，返回值是否实际发生改变。

        mpv 断开/加载文件时可能发出 ``None``，这类值不会覆盖上一次有效
        状态。未知属性也会被安全忽略，便于直接把所有 IPC 事件交给本方法。
        """

        attr = self._PROPERTY_MAP.get(name, name if name in self._PROPERTY_MAP.values() else None)
        if attr is None or value is None:
            return False
        if attr in ("sub_delay", "audio_delay"):
            normalized = self._number(value, getattr(self, attr))
        elif attr == "sub_scale":
            normalized = max(0.01, self._number(value, getattr(self, attr)))
        elif attr == "sub_pos":
            normalized = max(0.0, min(100.0, self._number(value, getattr(self, attr))))
        else:
            normalized = self._optional_number(value)

        callbacks: list[PropertyListener]
        with self._lock:
            old = getattr(self, attr)
            if old == normalized:
                return False
            setattr(self, attr, normalized)
            callbacks = list(self._listeners)
        for callback in callbacks:
            try:
                callback(name, normalized, self)
            except Exception:
                # 状态监听不能因为某个 UI 回调失败而阻塞 IPC 读取线程。
                continue
        return True

    def update(self, **values: Any) -> None:
        """批量更新，键可以是字段名或 mpv 属性名。"""

        for name, value in values.items():
            self.update_property(name, value)

    def snapshot(self) -> Dict[str, Any]:
        """返回线程安全的普通字典，适合传给导出线程。"""

        with self._lock:
            return {
                "sub_delay": self.sub_delay,
                "audio_delay": self.audio_delay,
                "sub_scale": self.sub_scale,
                "sub_font_size": self.sub_font_size,
                "sub_pos": self.sub_pos,
            }

    def to_mpv_properties(self) -> Dict[str, Any]:
        """转换为 mpv JSON/持久化文件使用的属性名。"""

        snap = self.snapshot()
        result = {
            "sub-delay": snap["sub_delay"],
            "audio-delay": snap["audio_delay"],
            "sub-scale": snap["sub_scale"],
            "sub-pos": snap["sub_pos"],
        }
        if snap["sub_font_size"] is not None:
            result["sub-font-size"] = snap["sub_font_size"]
        return result

    @classmethod
    def from_mpv_properties(cls, properties: Mapping[str, Any]) -> "PlaybackState":
        values = {field_name: properties[prop] for prop, field_name in cls._PROPERTY_MAP.items() if prop in properties}
        return cls(**values)

    def save(self, path: Union[str, os.PathLike[str]]) -> None:
        """将状态合并写入 JSON，保留 mpv/其它脚本已有的字段。"""

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        data: Dict[str, Any] = {}
        if target.is_file():
            try:
                loaded = json.loads(target.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data.update(loaded)
            except (OSError, ValueError, UnicodeDecodeError):
                pass
        data.update(self.to_mpv_properties())
        # 原子替换避免 mpv 同时读取时看到半个 JSON 文件。
        temporary = target.with_name(target.name + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        try:
            os.replace(temporary, target)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    @classmethod
    def load(cls, path: Union[str, os.PathLike[str]], default: Optional["PlaybackState"] = None) -> "PlaybackState":
        """从 mpv persistent_config.json 读取，文件不存在时返回默认值。"""

        result = default or cls()
        target = Path(path)
        if not target.is_file():
            return result
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                for name, value in loaded.items():
                    result.update_property(name, value)
        except (OSError, ValueError, UnicodeDecodeError):
            pass
        return result


# 全局实例作为主窗口与导出设置面板之间的同步中枢。get_playback_state 可用于
# 测试或需要注入独立状态的场景，因此没有把实例隐藏在 IPC 类内部。
playback_state = PlaybackState()


def get_playback_state() -> PlaybackState:
    return playback_state


def mpv_pos_to_ass_margin_v(
    sub_pos: float,
    *,
    play_res_y: int = 1080,
    font_size: float = 24,
    base_margin: int = 0,
) -> int:
    """将 mpv 的 ``sub-pos`` 百分比换算成 ASS 的 ``MarginV``。

    mpv 的坐标从顶部计算（0=顶端，100=底端），而 ASS ``MarginV`` 在底部
    对齐样式中是“距底边的像素数”。可用高度为 ``H - 字号 - 2*base``，
    因此：``MarginV = base + (100 - sub_pos)/100 * 可用高度``。这样
    ``sub_pos=100`` 落在底部安全边距，``sub_pos=0`` 落在顶部安全边距，
    中间值保持线性移动。导出端可将结果写入 ``force_style=MarginV=...``。
    """

    h = max(1, int(play_res_y))
    size = max(0, float(font_size))
    margin = max(0, int(base_margin))
    available = max(0.0, h - size - 2 * margin)
    position = max(0.0, min(100.0, float(sub_pos)))
    return max(0, int(round(margin + (100.0 - position) / 100.0 * available)))


class MpvJsonIpc:
    """mpv JSON IPC 客户端，负责 ``observe_property`` 与事件分发。

    ``connect`` 会启动后台读取线程并立即订阅四个状态属性；mpv 对
    ``observe_property`` 的响应会包含当前值，因此无需额外轮询即可初始化
    状态。关闭时 ``close`` 先同步读取所有属性，再持久化最后状态，避免用户
    关闭播放器窗口的瞬间丢失最后一次 OSD 调整。
    """

    OBSERVED_PROPERTIES: Tuple[str, ...] = ("sub-delay", "audio-delay", "sub-scale", "sub-pos")

    def __init__(
        self,
        endpoint: Union[str, os.PathLike[str], Tuple[str, int]],
        state: Optional[PlaybackState] = None,
        *,
        persistence_path: Optional[Union[str, os.PathLike[str]]] = None,
        connect_timeout: float = 2.0,
        request_timeout: float = 1.0,
    ) -> None:
        self.endpoint = endpoint
        self.state = state or get_playback_state()
        self.persistence_path = Path(persistence_path) if persistence_path else None
        self.connect_timeout = max(0.05, float(connect_timeout))
        self.request_timeout = max(0.05, float(request_timeout))
        self._transport: Any = None
        self._transport_lock = threading.Lock()
        # close() 可能同时由主窗口和 mpv 进程监视线程调用；命名管道在
        # Windows 上关闭时是阻塞操作，必须串行化。
        self._close_lock = threading.Lock()
        self._stop = threading.Event()
        self._reader_thread: Optional[threading.Thread] = None
        self._request_id = 0
        self._pending: Dict[int, Tuple[threading.Event, Dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._callbacks: list[Callable[[Dict[str, Any]], None]] = []

    @staticmethod
    def default_endpoint() -> str:
        """生成 Windows 命名管道路径；Unix 使用临时目录下的 socket。"""

        suffix = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        if os.name == "nt":
            return rf"\\.\pipe\video_to_srt_mpv-{suffix}"
        return str(Path(os.getenv("TMPDIR", "/tmp")) / f"video_to_srt_mpv-{suffix}.sock")

    @staticmethod
    def mpv_command(
        mpv_path: Union[str, os.PathLike[str]],
        media_path: Union[str, os.PathLike[str]],
        endpoint: Union[str, os.PathLike[str]],
        *,
        config_dir: Optional[Union[str, os.PathLike[str]]] = None,
    ) -> list[str]:
        """构造外部 mpv 启动参数，调用方再用 ``subprocess.Popen`` 启动。"""

        command = [str(mpv_path), f"--input-ipc-server={endpoint}"]
        if config_dir:
            command.append(f"--config-dir={config_dir}")
        command.append(str(media_path))
        return command

    def add_event_listener(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    def _open_transport(self) -> Any:
        endpoint = self.endpoint
        if isinstance(endpoint, tuple):
            host, port = endpoint
            sock = socket.create_connection((host, int(port)), timeout=self.connect_timeout)
            return sock.makefile("rwb", buffering=0)
        value = os.fspath(endpoint)
        if value.startswith("tcp://"):
            host_port = value[6:]
            host, _, port = host_port.rpartition(":")
            if not host or not port:
                raise ValueError(f"无效的 TCP IPC 端点: {value}")
            sock = socket.create_connection((host, int(port)), timeout=self.connect_timeout)
            return sock.makefile("rwb", buffering=0)
        if os.name == "nt" and value.startswith("\\\\.\\pipe\\"):
            # Python 的内置 open 可访问同步 Windows named pipe；读取放在独立
            # 线程，因此不会阻塞 GUI。mpv 在客户端断开后会返回 EOF。
            return open(value, "r+b", buffering=0)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.connect_timeout)
        sock.connect(value)
        return sock.makefile("rwb", buffering=0)

    def connect(self) -> None:
        if self._reader_thread and self._reader_thread.is_alive():
            return
        self._transport = self._open_transport()
        self._stop.clear()
        self._reader_thread = threading.Thread(target=self._reader_loop, name="mpv-json-ipc", daemon=True)
        self._reader_thread.start()
        for index, name in enumerate(self.OBSERVED_PROPERTIES):
            self.send_command("observe_property", index + 1, name)

    @property
    def connected(self) -> bool:
        return bool(self._reader_thread and self._reader_thread.is_alive() and self._transport is not None)

    def _write_message(self, payload: Mapping[str, Any]) -> None:
        raw = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._transport_lock:
            if self._transport is None:
                raise RuntimeError("mpv IPC 尚未连接")
            self._transport.write(raw)
            flush = getattr(self._transport, "flush", None)
            if flush:
                flush()

    def send_command(self, *command: Any, wait: bool = False, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """发送 mpv command；wait=True 时返回 command response。"""

        with self._pending_lock:
            self._request_id += 1
            request_id = self._request_id
            waiter = threading.Event()
            result: Dict[str, Any] = {}
            self._pending[request_id] = (waiter, result)
        try:
            self._write_message({"command": list(command), "request_id": request_id})
        except Exception:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise
        if not wait:
            return None
        if not waiter.wait(timeout if timeout is not None else self.request_timeout):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            return None
        return result

    def _readline(self) -> bytes:
        transport = self._transport
        if transport is None:
            return b""
        return transport.readline()

    def _reader_loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    raw = self._readline()
                except (OSError, ValueError):
                    # close() 关闭 named pipe/socket 后，readline 抛出的
                    # I/O operation on closed file 属于正常退出。
                    break
                if not raw:
                    break
                try:
                    message = json.loads(raw.decode("utf-8", errors="replace"))
                except (TypeError, ValueError):
                    continue
                if not isinstance(message, dict):
                    continue
                request_id = message.get("request_id")
                if request_id is not None:
                    with self._pending_lock:
                        pending = self._pending.pop(int(request_id), None)
                    if pending:
                        waiter, result = pending
                        result.update(message)
                        waiter.set()
                if message.get("event") == "property-change":
                    self.state.update_property(str(message.get("name", "")), message.get("data"))
                for callback in list(self._callbacks):
                    try:
                        callback(message)
                    except Exception:
                        continue
        finally:
            self._stop.set()

    def sync_state(self, timeout: Optional[float] = None) -> PlaybackState:
        """向 mpv 查询所有目标属性，确保关闭前拿到最后值。"""

        for name in self.OBSERVED_PROPERTIES:
            response = self.send_command("get_property", name, wait=True, timeout=timeout)
            if response and response.get("error") == "success":
                self.state.update_property(name, response.get("data"))
        return self.state

    def close(self, *, persist: bool = True, sync: bool = True) -> None:
        """停止读取并保存最后状态；可在 mpv 进程退出回调中调用。"""

        # mpv 进程监视线程也会在进程退出时调用 close；串行化整个关闭过程
        # 避免一个线程在 sync_state 时被另一个线程抢先关闭传输端点。
        with self._close_lock:
            if sync and self.connected:
                try:
                    self.sync_state()
                except (OSError, RuntimeError, ValueError):
                    pass
            if persist and self.persistence_path:
                self.state.save(self.persistence_path)
            self._stop.set()
            transport = self._transport
            self._transport = None
            if transport is not None:
                try:
                    transport.close()
                except OSError:
                    pass
            # Unix socket 是文件系统节点，mpv 退出后不会替 Python 客户端清理；
            # 删除它可避免下一次启动误连到旧端点。Windows 命名管道不需要此步。
            if isinstance(self.endpoint, (str, os.PathLike)):
                endpoint_path = os.fspath(self.endpoint)
                if not endpoint_path.startswith("tcp://") and not endpoint_path.startswith("\\\\.\\pipe\\"):
                    try:
                        os.unlink(endpoint_path)
                    except OSError:
                        pass
            thread = self._reader_thread
            if thread and thread is not threading.current_thread():
                thread.join(timeout=0.5)
            self._reader_thread = None
            with self._pending_lock:
                for waiter, _ in self._pending.values():
                    waiter.set()
                self._pending.clear()

    def __enter__(self) -> "MpvJsonIpc":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()


def playback_state_values(value: Any) -> Dict[str, Any]:
    """返回导出侧需要的字段，兼容状态对象、快照字典和普通映射。

    ``PlaybackState.snapshot()`` 当前返回字典，而旧集成可能传入状态对象；
    导出模块通过这个适配函数避免依赖具体实现。
    """

    names = ("sub_delay", "audio_delay", "sub_scale", "sub_font_size", "sub_pos")
    if value is None:
        return PlaybackState().snapshot()
    if isinstance(value, Mapping):
        source = value
        return PlaybackState.from_mpv_properties(source).snapshot() if any(key in source for key in PlaybackState._PROPERTY_MAP) else {
            name: source.get(name, getattr(PlaybackState(), name)) for name in names
        }
    snapshot = getattr(value, "snapshot", None)
    if callable(snapshot):
        current = snapshot()
        if isinstance(current, Mapping):
            return playback_state_values(current)
    return {name: getattr(value, name, getattr(PlaybackState(), name)) for name in names}


def default_persistence_path() -> Path:
    """返回项目随附便携版 mpv 的 persistent_config.json 路径。"""

    return Path(__file__).resolve().parent / "mpv" / "portable_config" / "persistent_config.json"


__all__ = [
    "MpvJsonIpc",
    "PlaybackState",
    "default_persistence_path",
    "get_playback_state",
    "mpv_pos_to_ass_margin_v",
    "playback_state_values",
    "playback_state",
]
