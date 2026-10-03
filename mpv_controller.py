"""便携版 mpv 的启动与状态同步控制器。

``main.py`` 旧实现使用 ``subprocess.call``，既会阻塞 GUI，也无法读取 OSD
菜单修改的属性。``MpvController`` 用 ``Popen`` 启动 mpv，并为每个实例分配
本机 JSON IPC 端点（Windows 命名管道 / Unix socket）；``MpvJsonIpc`` 在后台线程订阅
``sub-delay``、``audio-delay``、``sub-scale``、``sub-pos``，状态统一写入
``PlaybackState``。若打包的 mpv 只允许 Windows named pipe，可把
``ipc_endpoint`` 显式传为 ``\\\\.\\pipe\\name``，底层客户端同样支持。
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional, Union

from playback_state import (
    MpvJsonIpc,
    PlaybackState,
    default_persistence_path,
    get_playback_state,
)


class MpvController:
    """管理一个外部 mpv 进程及其 JSON IPC 状态同步。"""

    def __init__(
        self,
        mpv_path: Optional[Union[str, os.PathLike[str]]] = None,
        *,
        config_dir: Optional[Union[str, os.PathLike[str]]] = None,
        state: Optional[PlaybackState] = None,
        persistence_path: Optional[Union[str, os.PathLike[str]]] = None,
    ) -> None:
        root = Path(__file__).resolve().parent
        self.mpv_path = Path(mpv_path) if mpv_path else root / "mpv" / ("mpv.exe" if os.name == "nt" else "mpv")
        self.config_dir = Path(config_dir) if config_dir else root / "mpv" / "portable_config"
        self.state = state or get_playback_state()
        self.persistence_path = Path(persistence_path) if persistence_path else default_persistence_path()
        self.process: Optional[subprocess.Popen] = None
        self.ipc: Optional[MpvJsonIpc] = None
        self.endpoint: Optional[str] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()

    @property
    def running(self) -> bool:
        return bool(self.process and self.process.poll() is None)

    def start(
        self,
        media_path: Union[str, os.PathLike[str]],
        *,
        ipc_endpoint: Optional[Union[str, os.PathLike[str]]] = None,
        connect_timeout: float = 5.0,
    ) -> PlaybackState:
        """异步启动 mpv 并连接 IPC，返回共享状态。

        ``connect_timeout`` 只限制等待 mpv IPC 建立的时间；视频播放过程由
        mpv 自己运行，调用线程不会被视频时长阻塞。
        """

        with self._lock:
            self.close(terminate=True, persist=False)
            # mpv 原生 IPC 在 Windows 使用命名管道、Unix 使用 Unix
            # domain socket。TCP 端点仍可通过 ipc_endpoint 显式传入，便于
            # 测试，但默认走原生端点以保证便携版 mpv 真正能够创建它。
            self.endpoint = os.fspath(ipc_endpoint) if ipc_endpoint else MpvJsonIpc.default_endpoint()
            command = MpvJsonIpc.mpv_command(
                self.mpv_path,
                media_path,
                self.endpoint,
                config_dir=self.config_dir,
            )
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            ipc = MpvJsonIpc(
                self.endpoint,
                self.state,
                persistence_path=self.persistence_path,
                connect_timeout=min(1.0, max(0.05, connect_timeout)),
            )
            deadline = time.monotonic() + max(0.05, float(connect_timeout))
            last_error: Optional[Exception] = None
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(f"mpv 启动失败（退出码 {self.process.returncode}）")
                try:
                    ipc.connect()
                    break
                except (OSError, ValueError, RuntimeError) as exc:
                    last_error = exc
                    time.sleep(0.05)
            else:
                self.process.terminate()
                raise RuntimeError(f"无法连接 mpv JSON IPC: {last_error}")
            self.ipc = ipc
            self._monitor_thread = threading.Thread(target=self._monitor_process, name="mpv-process-monitor", daemon=True)
            self._monitor_thread.start()
            return self.state

    def _monitor_process(self) -> None:
        process = self.process
        if process is None:
            return
        process.wait()
        # 进程已经退出，命名管道可能不再接受新的 get_property 写入；
        # 退出前的 property-change 已由 IPC 读取线程处理，因此这里关闭时
        # 不再发送同步请求，避免 Windows named pipe 写端永久阻塞。
        ipc = self.ipc
        if ipc is not None:
            ipc.close(persist=True, sync=False)

    def command(self, *command: object, wait: bool = False):
        """向当前 mpv 发送 JSON IPC command。"""

        if not self.ipc:
            raise RuntimeError("mpv 尚未启动")
        return self.ipc.send_command(*command, wait=wait)

    def close(self, *, terminate: bool = False, persist: bool = True) -> None:
        """关闭 IPC；terminate=True 时同时请求结束 mpv 进程。"""

        with self._lock:
            ipc = self.ipc
            self.ipc = None
            process = self.process
            self.process = None
            # Windows named pipe 的 close() 可能等待 reader 线程；先结束 mpv
            # 让服务端关闭管道，再关闭客户端句柄可避免 GUI 永久卡住。进程
            # 仍在时先同步属性，保证最后一次 OSD 调整进入 PlaybackState。
            if ipc is not None and terminate and process is not None and process.poll() is None:
                try:
                    ipc.sync_state(timeout=min(0.5, ipc.request_timeout))
                except (OSError, RuntimeError, ValueError):
                    pass
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                ipc.close(persist=persist, sync=False)
            elif ipc is not None:
                ipc.close(persist=persist, sync=True)
            monitor = self._monitor_thread
            self._monitor_thread = None
            if monitor and monitor is not threading.current_thread():
                monitor.join(timeout=0.5)

    def __enter__(self) -> "MpvController":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close(terminate=True)


__all__ = ["MpvController"]
