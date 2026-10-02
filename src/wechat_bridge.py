"""微信桥接模块：通过 wxauto (UI Automation) 连接微信桌面客户端"""

import asyncio
import ctypes
import html
import hashlib
import importlib.util
import multiprocessing
import os
import queue
import random
import re
import subprocess
import time
import threading
from ctypes import wintypes
from typing import Callable, Awaitable
from dataclasses import dataclass
from enum import Enum

from loguru import logger


def _running_process_names() -> set[str]:
    """Return lower-case executable names without relying on tasklist.

    Some Windows sessions deny access to tasklist even though the current
    user can inspect processes through the Win32 API.  That made a healthy
    WeChat client look like it was not running after a restart.
    """
    names: set[str] = set()
    if os.name == "nt":
        try:
            TH32CS_SNAPPROCESS = 0x00000002
            INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

            class PROCESSENTRY32W(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            kernel32.CreateToolhelp32Snapshot.argtypes = [
                wintypes.DWORD,
                wintypes.DWORD,
            ]
            kernel32.Process32FirstW.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(PROCESSENTRY32W),
            ]
            kernel32.Process32NextW.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(PROCESSENTRY32W),
            ]
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

            snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if snapshot != INVALID_HANDLE_VALUE:
                try:
                    entry = PROCESSENTRY32W()
                    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
                    has_entry = bool(
                        kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
                    )
                    while has_entry:
                        names.add(entry.szExeFile.lower())
                        has_entry = bool(
                            kernel32.Process32NextW(
                                snapshot,
                                ctypes.byref(entry),
                            )
                        )
                finally:
                    kernel32.CloseHandle(snapshot)
        except Exception as e:
            logger.debug(f"通过 Win32 API 读取进程失败: {e}")

    if names:
        return names

    # Last-resort fallback for environments where the snapshot API is blocked.
    try:
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            timeout=5,
        )
        output = result.stdout.decode("utf-8", errors="ignore").lower()
        for line in output.splitlines():
            first = line.split(",", 1)[0].strip().strip('"')
            if first:
                names.add(first)
    except Exception as e:
        logger.debug(f"回退读取微信进程失败: {e}")
    return names


def _visible_window_process_ids(executable_names: set[str]) -> set[int]:
    """Find visible top-level window owners for the requested processes."""
    pids: set[int] = set()
    if os.name != "nt":
        return pids

    wanted = {name.lower() for name in executable_names}
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD

        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        enum_proc = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HWND,
            wintypes.LPARAM,
        )

        def callback(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            if user32.GetWindowTextLengthW(hwnd) <= 0:
                return True

            process_id = wintypes.DWORD()
            user32.GetWindowThreadProcessId(
                hwnd,
                ctypes.byref(process_id),
            )
            if not process_id.value:
                return True

            handle = kernel32.OpenProcess(
                0x1000,  # PROCESS_QUERY_LIMITED_INFORMATION
                False,
                process_id.value,
            )
            if not handle:
                return True
            try:
                size = wintypes.DWORD(1024)
                buffer = ctypes.create_unicode_buffer(size.value)
                if kernel32.QueryFullProcessImageNameW(
                    handle,
                    0,
                    buffer,
                    ctypes.byref(size),
                ):
                    executable = os.path.basename(buffer.value).lower()
                    if executable in wanted:
                        pids.add(process_id.value)
            finally:
                kernel32.CloseHandle(handle)
            return True

        user32.EnumWindows(enum_proc(callback), 0)
    except Exception as e:
        logger.debug(f"读取微信窗口进程失败: {e}")
    return pids


def _capture_window_region(
    hwnd: int,
    box: tuple[int, int, int, int] | None = None,
):
    """Capture a window with PrintWindow and crop absolute screen coordinates.

    PIL's ImageGrab depends on an attached interactive desktop. Remote,
    locked, or service-backed Windows sessions can deny that API even though
    the target window is alive. PrintWindow works through the window's device
    context in those sessions and keeps OCR available.
    """
    from PIL import Image
    import win32gui
    import win32ui

    if not hwnd or not win32gui.IsWindow(hwnd):
        raise RuntimeError("微信窗口句柄无效")

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise RuntimeError("微信窗口尺寸无效")

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    if not hwnd_dc:
        raise RuntimeError("无法获取微信窗口设备上下文")

    source_dc = None
    memory_dc = None
    bitmap = None
    try:
        source_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        memory_dc = source_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(source_dc, width, height)
        memory_dc.SelectObject(bitmap)

        flags = getattr(win32gui, "PW_RENDERFULLCONTENT", 0x00000002)
        captured = bool(
            ctypes.windll.user32.PrintWindow(
                hwnd,
                memory_dc.GetSafeHdc(),
                flags,
            )
        )
        if not captured:
            captured = bool(
                ctypes.windll.user32.PrintWindow(
                    hwnd,
                    memory_dc.GetSafeHdc(),
                    0,
                )
            )
        if not captured:
            raise RuntimeError("PrintWindow 截图失败")

        info = bitmap.GetInfo()
        data = bitmap.GetBitmapBits(True)
        image = Image.frombuffer(
            "RGB",
            (info["bmWidth"], info["bmHeight"]),
            data,
            "raw",
            "BGRX",
            0,
            1,
        )

        if box is not None:
            crop_left = max(left, int(box[0]))
            crop_top = max(top, int(box[1]))
            crop_right = min(right, int(box[2]))
            crop_bottom = min(bottom, int(box[3]))
            if crop_right <= crop_left or crop_bottom <= crop_top:
                raise RuntimeError("截图区域不在微信窗口内")
            image = image.crop(
                (
                    crop_left - left,
                    crop_top - top,
                    crop_right - left,
                    crop_bottom - top,
                )
            )

        # Some remote sessions return an all-black DC without reporting an
        # error. Treat that as a failed capture so the next fallback runs.
        if max(image.convert("L").getextrema()) == 0:
            raise RuntimeError("PrintWindow 返回空白图像")
        return image
    finally:
        if bitmap is not None:
            try:
                win32gui.DeleteObject(bitmap.GetHandle())
            except Exception:
                pass
        if memory_dc is not None:
            try:
                memory_dc.DeleteDC()
            except Exception:
                pass
        if source_dc is not None:
            try:
                source_dc.DeleteDC()
            except Exception:
                pass
        try:
            win32gui.ReleaseDC(hwnd, hwnd_dc)
        except Exception:
            pass


def _install_wechatauto_screenshot_compat() -> None:
    """Make wechatauto OCR survive RDP and restricted desktop sessions."""
    try:
        from wechatauto.guia import WeChatGUI
    except Exception as e:
        logger.debug(f"加载 wechatauto 截图兼容层失败: {e}")
        return

    if getattr(WeChatGUI, "_wechatbot_safe_screenshot", False):
        return

    def safe_grab_screen(self, box=None):
        from PIL import ImageGrab

        errors: list[str] = []
        last_error: Exception | None = None

        for delay in (0.0, 0.12):
            if delay:
                time.sleep(delay)
            try:
                return (
                    ImageGrab.grab(bbox=box, all_screens=True)
                    if box
                    else ImageGrab.grab(all_screens=True)
                )
            except Exception as exc:
                last_error = exc
                errors.append(f"桌面截图: {exc}")

        hwnd = int(getattr(self, "main_hwnd", 0) or 0)
        try:
            return _capture_window_region(hwnd, box)
        except Exception as exc:
            last_error = exc
            errors.append(f"窗口截图: {exc}")

        # A disconnected RDP session can leave the window iconized. Restore
        # it and retry both capture paths once before surfacing the error.
        try:
            if hwnd:
                user32 = ctypes.windll.user32
                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, 9)
                else:
                    user32.ShowWindow(hwnd, 5)
                user32.SetForegroundWindow(hwnd)
                time.sleep(0.2)
        except Exception as exc:
            errors.append(f"唤醒窗口: {exc}")

        try:
            return _capture_window_region(hwnd, box)
        except Exception as exc:
            last_error = exc
            errors.append(f"窗口重试: {exc}")

        try:
            return (
                ImageGrab.grab(bbox=box, all_screens=True)
                if box
                else ImageGrab.grab(all_screens=True)
            )
        except Exception as exc:
            last_error = exc
            errors.append(f"桌面重试: {exc}")

        logger.debug("截图兼容层尝试失败: " + "；".join(errors))
        raise RuntimeError(f"屏幕截图失败：{last_error}")

    WeChatGUI._grab_screen = safe_grab_screen
    WeChatGUI._wechatbot_safe_screenshot = True


def _install_wechatauto_uia_compat() -> None:
    """Keep UIA usable when Windows blocks third-party module enumeration.

    The packaged wechatauto driver filters visible windows by inspecting
    ``Weixin.dll``.  Some desktop sessions deny that module scan even though
    UI Automation itself works, which makes a logged-in WeChat look absent.
    The fallback below only enumerates visible windows owned by Weixin.exe and
    never clicks, scans, or otherwise handles a login prompt.
    """
    _install_wechatauto_screenshot_compat()

    try:
        from wechatauto.uia_driver import WeChatUIA
    except Exception as e:
        logger.debug(f"加载 wechatauto UIA 兼容层失败: {e}")
        return

    if getattr(WeChatUIA, "_wechatbot_safe_hwnds", False):
        return

    original = WeChatUIA._wechat_hwnds

    def safe_process_modules(pid: int):
        """Yield module tuples through PSAPI when Toolhelp is restricted."""
        try:
            import win32api
            import win32con
            import win32process

            handle = win32api.OpenProcess(
                win32con.PROCESS_QUERY_LIMITED_INFORMATION
                | win32con.PROCESS_VM_READ,
                False,
                pid,
            )
        except Exception:
            return

        if not handle:
            return

        try:
            modules = win32process.EnumProcessModulesEx(handle, 0x03)
            for module in modules:
                try:
                    path = win32process.GetModuleFileNameEx(handle, module)
                    yield (
                        int(module),
                        0,
                        os.path.basename(path),
                        path,
                    )
                except Exception:
                    continue
        finally:
            try:
                win32api.CloseHandle(handle)
            except Exception:
                pass

    def safe_wechat_hwnds(self):
        try:
            handles = original(self)
        except Exception:
            handles = []
        if handles:
            return handles

        try:
            import psutil
            import win32gui
            import win32process

            weixin_pids = {
                proc.pid
                for proc in psutil.process_iter(["name"])
                if (proc.info.get("name") or "").lower() == "weixin.exe"
            }
            if not weixin_pids:
                return []

            candidates = []

            def collect(hwnd, _):
                try:
                    if not win32gui.IsWindowVisible(hwnd):
                        return True
                    title = win32gui.GetWindowText(hwnd) or ""
                    if not any(hint in title for hint in ("微信", "Weixin")):
                        return True
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    if pid not in weixin_pids:
                        return True
                    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                    area = max(0, right - left) * max(0, bottom - top)
                    candidates.append((area, hwnd))
                except Exception:
                    pass
                return True

            win32gui.EnumWindows(collect, None)
            candidates.sort(reverse=True)
            return [hwnd for _, hwnd in candidates]
        except Exception as e:
            logger.debug(f"兼容层枚举微信窗口失败: {e}")
            return []

    WeChatUIA._wechat_hwnds = safe_wechat_hwnds
    WeChatUIA._process_modules = staticmethod(safe_process_modules)
    WeChatUIA._wechatbot_safe_hwnds = True
    # The bot never performs login.  This also protects older library paths
    # that would otherwise click a login button when a login window appears.
    WeChatUIA._auto_login = lambda self: False


class ConnectionStatus(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


@dataclass
class WeChatMessage:
    """微信消息"""
    id: str                     # 消息ID
    sender: str                 # 发送者显示名
    sender_name: str            # 发送者昵称
    room_id: str                # 群聊名称（用作唯一标识）
    room_name: str              # 群聊名称
    content: str                # 消息内容
    type: int                   # 消息类型（1=文本）
    timestamp: float            # 时间戳
    is_group: bool              # 是否群消息
    is_at_me: bool              # 是否@我
    message_type: str = "text"
    is_private: bool = False    # 是否私聊消息
    is_target_group: bool = True  # 群消息是否属于配置里要回应的群
    is_poke_me: bool = False    # 是否有人拍一拍/戳一戳机器人
    is_self: bool = False       # 是否机器人自己发出的消息
    poke_sender: str = ""       # 拍一拍发起人的显示名
    is_image_request: bool = False  # 是否要求机器人联网找图片
    voice_path: str = ""        # 已提取到本地的语音文件（供 ASR 使用）
    voice_url: str = ""         # 远端语音附件地址（QQ）
    voice_mime: str = ""        # 语音附件 MIME
    voice_filename: str = ""    # 远端语音附件文件名


def _control_bounds(control) -> tuple[int, int, int, int] | None:
    """返回可见控件的屏幕矩形；控件不可见或尺寸异常时返回 None。"""
    try:
        if bool(getattr(control, "IsOffscreen", False)):
            return None
        rect = control.BoundingRectangle
        left, top = int(rect.left), int(rect.top)
        right, bottom = int(rect.right), int(rect.bottom)
    except Exception:
        return None

    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None
    return left, top, right, bottom


def _iter_uia_controls(root, max_depth: int = 40, max_nodes: int = 5000):
    """遍历 UIA 控件树，带深度和节点数限制。"""
    if root is None:
        return

    stack = [(root, 0)]
    visited = 0
    while stack and visited < max_nodes:
        control, depth = stack.pop()
        visited += 1
        yield control
        if depth >= max_depth:
            continue
        try:
            children = control.GetChildren()
        except Exception:
            continue
        for child in reversed(children):
            stack.append((child, depth + 1))


def _find_uia_controls(root, predicate, max_depth: int = 40) -> list:
    """返回控件树中所有满足条件的控件。"""
    return [
        control
        for control in _iter_uia_controls(root, max_depth=max_depth)
        if predicate(control)
    ]


class WeChatBridge:
    """微信桥接器：通过 UI Automation 监听群消息、发送回复"""

    SEND_TIMEOUT_SECONDS = 5.0           # 单次发送最多等待 5 秒（避免频繁超时）
    SEND_MIN_INTERVAL_SECONDS = 0.5      # 两条外发消息之间的最小间隔（0.5秒足够快）
    SEND_BACKOFF_BASE_SECONDS = 0.3      # 发送异常后的短冷却（更快恢复）
    SEND_BACKOFF_MAX_SECONDS = 2.0       # 冷却最长 2 秒（减少等待）
    SEND_ITEM_TTL_SECONDS = 60.0         # 排队太久没发出去的回复直接丢弃
    MESSAGE_DEDUPE_WINDOW_SECONDS = 60.0 # 去重窗口60秒（避免重复但不过长）

    # 这些账号的消息不参与私聊自动回复（公众号/系统号/文件传输助手等）
    IGNORED_PRIVATE_ACCOUNTS = {
        "filehelper",
        "newsapp",
        "fmessage",
        "floatbottle",
        "medianote",
        "notifymessage",
        "qqmail",
        "tmessage",
        "weixin",
        "mphelper",
        "weixinnotify",
    }

    def __init__(
        self,
        target_groups: list[str],
        on_message: Callable[[WeChatMessage], Awaitable[None]] | None = None,
        reply_delay: float = 0.0,
        backend: str = "auto",
        listen_private: bool = True,
    ):
        self.target_groups = target_groups
        self.on_message = on_message
        self.reply_delay = reply_delay
        self.backend = backend
        self.listen_private = bool(listen_private)
        self._wx = None
        self._backend = ""
        self._status = ConnectionStatus.DISCONNECTED
        self._running = False
        self._poll_thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._room_names_by_id: dict[str, str] = {}
        self._listen_keys_by_group: dict[str, str] = {}
        self._registered_listen_keys_by_group: dict[str, str] = {}
        self._bot_names: set[str] = set()
        self._self_wxid = ""
        self._chat_names: dict[str, str] = {}
        self._target_group_ids: set[str] = set()
        self._listen_all_active = False
        self._uia_available = False
        # 发送隔离：所有外发消息由单独线程串行处理，
        # 微信界面卡住时只会丢弃本次发送，不会拖垮整个机器人
        self._send_queue: queue.Queue = queue.Queue(maxsize=8)
        self._sender_thread: threading.Thread | None = None
        self._sender_busy = threading.Event()
        self._sender_busy_since = 0.0
        self._accepting_sends = True
        self._send_gate: asyncio.Lock | None = None
        self._last_send_at = 0.0
        self._send_backoff_until = 0.0
        self._send_failures = 0
        self._recent_sent: dict[tuple[str, str], float] = {}
        self._send_inflight: dict[tuple[str, str], float] = {}
        self._send_inflight_lock = threading.Lock()
        self._seen_message_ids: dict[str, float] = {}
        self._window_process_ids: set[int] = set()

    @property
    def status(self) -> ConnectionStatus:
        return self._status

    @property
    def is_connected(self) -> bool:
        return self._status == ConnectionStatus.CONNECTED

    async def connect(self) -> bool:
        """连接用户已经手动登录的微信窗口。"""
        try:
            self._status = ConnectionStatus.CONNECTING
            logger.info("正在通过 UI Automation 连接微信...")

            try:
                self._backend = self._resolve_backend()
            except RuntimeError as e:
                logger.error(str(e))
                self._status = ConnectionStatus.ERROR
                return False

            # WeChat() 是同步阻塞调用，在线程池中执行避免阻塞 asyncio
            loop = asyncio.get_event_loop()
            if self._backend == "wechatauto":
                _install_wechatauto_uia_compat()
                from wechatauto import WeChat

                self._wx = await loop.run_in_executor(
                    None,
                    lambda: WeChat(start_listener=False),
                )
            elif self._backend == "wxauto4":
                from wxauto4 import WeChat, WxParam

                WxParam.TELEMETRY_ENABLED = False
                self._wx = await loop.run_in_executor(
                    None,
                    lambda: WeChat(
                        ads=False,
                        resize=False,
                        start_listener=False,
                    ),
                )
            else:
                from wxauto import WeChat

                self._wx = await loop.run_in_executor(None, WeChat)

            self._load_bot_identity()
            if self._backend == "wechatauto":
                try:
                    uia_ready = await asyncio.wait_for(
                        loop.run_in_executor(
                            None,
                            self.prepare_uia,
                        ),
                        timeout=10.0,
                    )
                except asyncio.TimeoutError:
                    uia_ready = False
                    logger.warning(
                        "微信 UIA 初始化超过 10 秒，切换到 OCR 兼容模式"
                    )
                if not uia_ready and not self.has_usable_gui():
                    logger.error(
                        "微信聊天主窗口不可用，"
                        "请确认已手动登录微信且窗口可见"
                    )
                    self._status = ConnectionStatus.ERROR
                    return False
                if not uia_ready:
                    logger.warning(
                        "微信 UIA 树不可用，已启用 OCR 兼容模式；"
                        "仍会监听并回复消息"
                    )
            self._window_process_ids = _visible_window_process_ids(
                self._window_executable_names()
            )
            self._accepting_sends = True
            self._status = ConnectionStatus.CONNECTED
            logger.info(f"微信连接成功（{self._backend}）")
            return True

        except Exception as e:
            error_msg = str(e)
            if "SetWindowPos" in error_msg or "窗口" in error_msg or "1400" in error_msg:
                logger.error(
                    "微信连接失败: 找不到可用聊天窗口。"
                    "请由用户手动登录微信并保持窗口可见"
                )
            else:
                logger.error(f"微信连接失败: {e}")
            self._status = ConnectionStatus.ERROR
            return False

    def _window_executable_names(self) -> set[str]:
        if self._backend in {"wechatauto", "wxauto4"}:
            return {"weixin.exe"}
        return {"wechat.exe"}

    def has_usable_uia(self) -> bool:
        """Return True when the chat window exposes a usable UIA tree."""
        if not self._uia_available:
            return False
        gui = getattr(self._wx, "_gui", None)
        if gui is None:
            return False
        try:
            uia = gui._get_uia()
            return bool(uia is not None and uia.is_materialized())
        except Exception as e:
            logger.debug(f"检测微信 UIA 可用性失败: {e}")
            return False

    def has_usable_gui(self) -> bool:
        """Return True when the GUI bridge has a live, visible window."""
        gui = getattr(self._wx, "_gui", None)
        if gui is None:
            return False
        try:
            hwnd = int(
                getattr(gui, "main_hwnd", 0)
                or getattr(gui, "_hwnd", 0)
                or getattr(gui, "render_hwnd", 0)
                or 0
            )
            if not hwnd:
                return False
            user32 = ctypes.windll.user32
            if not user32.IsWindow(hwnd):
                return False
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                time.sleep(0.4)
            if not user32.IsWindowVisible(hwnd):
                user32.ShowWindow(hwnd, 5)  # SW_SHOW
                time.sleep(0.2)
            return bool(
                user32.IsWindowVisible(hwnd)
                and not user32.IsIconic(hwnd)
            )
        except Exception as e:
            logger.debug(f"检测微信 GUI 可用性失败: {e}")
            return False

    def prepare_uia(self) -> bool:
        """Initialize the UIA tree of a window that the user already logged in."""
        self._uia_available = False
        gui = getattr(self._wx, "_gui", None)
        if gui is None:
            return False
        try:
            uia = gui._get_uia(refresh=True)
            if uia is None:
                return False
            if not uia._wechat_hwnds():
                return False
            if not uia.is_materialized():
                uia.ensure_materialized(
                    timeout=8,
                    force=True,
                )
            main = uia._find_main()
            if main is None:
                return False
            uia._win = main
            uia._activate(main)
            self._uia_available = True
            return True
        except Exception as e:
            logger.debug(f"预初始化微信 UIA 失败: {e}")
            return False

    def window_process_changed(self) -> bool:
        """Detect a WeChat client restart while the bot still holds old UIA objects."""
        if self._status != ConnectionStatus.CONNECTED:
            return False

        current = _visible_window_process_ids(self._window_executable_names())
        if not current:
            return False

        if self._window_process_ids and not (
            current & self._window_process_ids
        ):
            logger.warning(
                "检测到微信主窗口进程已变化: "
                f"{sorted(self._window_process_ids)} -> {sorted(current)}"
            )
            return True

        if not self._window_process_ids:
            self._window_process_ids = current
        return False

    def is_sender_stuck(self, timeout: float = 20.0) -> bool:
        """Return True when a UIA send has not returned for too long."""
        if not self._sender_busy.is_set() or self._sender_busy_since <= 0:
            return False
        return time.monotonic() - self._sender_busy_since >= timeout

    def abandon_stuck_sender(self) -> bool:
        """Detach the current sender thread and reset its global UIA lock.

        A blocked UIA call cannot be cancelled in-process.  Giving the new
        connection a fresh queue/event/lock lets the bot recover instead of
        waiting forever behind the old WeChat window.
        """
        if not self._sender_busy.is_set():
            return False

        old_sender = self._sender_thread
        self._send_queue = queue.Queue(maxsize=8)
        self._sender_thread = None
        self._sender_busy = threading.Event()
        self._sender_busy_since = 0.0

        try:
            from wechatauto.utils.lock import LockManager

            # The old sender may still hold this object forever.  New UIA
            # work must not wait behind that stale lock.
            LockManager.process_lock = multiprocessing.Lock()
            LockManager.thread_lock = threading.RLock()
        except Exception as e:
            logger.debug(f"重置微信自动化锁失败: {e}")

        logger.warning(
            "已隔离卡死的微信发送线程"
            + (f" ({old_sender.name})" if old_sender else "")
        )
        return True

    async def recover_connection(self) -> bool:
        """Reconnect after WeChat restarts or a UIA send thread gets stuck."""
        self.abandon_stuck_sender()

        try:
            await asyncio.wait_for(self.disconnect(), timeout=18)
        except asyncio.TimeoutError:
            logger.warning("自动恢复时断开旧微信连接超时，继续尝试重连")
        except Exception as e:
            logger.warning(f"自动恢复时断开旧微信连接失败: {e}")

        if not await self.connect():
            return False

        self._send_failures = 0
        self._send_backoff_until = 0.0
        self.start_listening(asyncio.get_running_loop())
        return (
            self._status == ConnectionStatus.CONNECTED
            and self._running
        )

    def _resolve_backend(self) -> str:
        """根据微信进程和显式配置选择自动化后端。"""
        requested = self.backend.lower().strip()
        aliases = {
            "wechatauto": "wechatauto",
            "wechatauto-replica": "wechatauto",
            "weixin4": "wechatauto",
            "wxauto4": "wxauto4",
            "weixin": "wechatauto",
            "4": "wechatauto",
            "wxauto": "wxauto",
            "wechat": "wxauto",
            "3.9": "wxauto",
        }

        processes = _running_process_names()

        if requested != "auto":
            backend = aliases.get(requested, requested)
            if backend not in {"wxauto", "wxauto4", "wechatauto"}:
                raise RuntimeError(f"不支持的微信后端: {self.backend}")
        elif "weixin.exe" in processes:
            backend = (
                "wechatauto"
                if importlib.util.find_spec("wechatauto") is not None
                else "wxauto4"
            )
        elif "wechat.exe" in processes:
            backend = "wxauto"
        else:
            raise RuntimeError(
                "未检测到微信进程，请由用户先打开并登录微信桌面客户端"
            )

        module_name = backend
        if importlib.util.find_spec(module_name) is None:
            packages = {
                "wechatauto": "wechatauto-replica",
                "wxauto4": "wxauto4",
                "wxauto": "wxauto",
            }
            package = packages[backend]
            raise RuntimeError(f"{package} 未安装，请重新运行本地部署安装命令")

        logger.info(f"自动选择微信自动化后端: {backend}")
        return backend

    def _load_bot_identity(self) -> None:
        """记录当前微信昵称，用于识别群消息中的 @机器人。"""
        names: set[str] = set()
        nickname = getattr(self._wx, "nickname", None)
        if nickname:
            names.add(str(nickname).strip())

        try:
            info = self._wx._db.get_self_info()
            self_wxid = str(info.get("username") or "").strip()
            if self_wxid:
                self._self_wxid = self_wxid
            for key in ("nick_name", "remark", "username"):
                value = info.get(key)
                if value:
                    names.add(str(value).strip())
        except Exception:
            pass

        self._bot_names = {name for name in names if name}
        if self._bot_names:
            logger.info(f"已识别机器人微信昵称: {', '.join(sorted(self._bot_names))}")

    def start_listening(self, loop: asyncio.AbstractEventLoop) -> None:
        """启动消息监听（轮询模式）"""
        if not self._wx or self._status != ConnectionStatus.CONNECTED:
            logger.error("微信未连接，无法启动监听")
            return

        self._running = True
        self._loop = loop
        self._room_names_by_id.clear()
        self._listen_keys_by_group.clear()
        self._registered_listen_keys_by_group.clear()
        self._chat_names.clear()
        self._target_group_ids.clear()
        self._listen_all_active = False

        # 先把配置里的群名解析成群 wxid，用于后续过滤非目标群消息
        resolved_groups: list[tuple[str, str]] = []
        for group in self.target_groups:
            group_id = group
            if self._backend == "wechatauto":
                group_id = self._resolve_wechatauto_group_id(group) or ""
                if not group_id:
                    logger.error(
                        f"未在微信数据库中群聊 [{group}]，已跳过注册，"
                        "避免误监听同名联系人"
                    )
                    continue
            self._room_names_by_id[group_id] = group
            self._listen_keys_by_group[group] = group_id
            self._target_group_ids.add(group_id)
            resolved_groups.append((group, group_id))

        self._registered_listen_keys_by_group.update(
            self._listen_keys_by_group
        )

        # wechatauto 支持全局监听：一次监听所有会话（含私聊与新群），
        # 因此私聊消息也能进入处理管线，不再只监听配置里的群。
        if self._backend == "wechatauto" and self.listen_private:
            try:
                self._wx.AddListenAll(
                    callback=self._handle_callback_message,
                )
                self._listen_all_active = True
                self._wx.StartListening()
                logger.info(
                    "消息监听已启动（全局监听：私聊 + 全部会话，"
                    f"仅对配置群聊回应 @/关键词）: "
                    f"{', '.join(group for group, _ in resolved_groups) or '无'}"
                )
                return
            except Exception as e:
                logger.error(f"启动全局消息监听失败，回退为按群监听: {e}")
                self._listen_all_active = False

        # 按群注册监听（非 wechatauto 后端，或全局监听不可用时）
        for group, listen_key in resolved_groups:
            try:
                if self._backend in {"wxauto4", "wechatauto"}:
                    self._wx.AddListenChat(
                        listen_key,
                        callback=self._handle_callback_message,
                    )
                else:
                    self._wx.AddListenChat(who=group)
                if listen_key == group:
                    logger.info(f"已注册监听群聊: {group}")
                else:
                    logger.info(f"已注册监听群聊: {group} ({listen_key})")
            except Exception as e:
                logger.warning(f"注册监听群聊失败 [{group}]: {e}")

        if self._backend in {"wxauto4", "wechatauto"}:
            try:
                self._wx.StartListening()
            except Exception as e:
                logger.error(f"启动 {self._backend} 消息监听失败: {e}")
                self._running = False
                return
            logger.info(f"消息监听已启动（{self._backend} 回调模式）")
            return

        # 启动轮询线程
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name="wechat-poller",
        )
        self._poll_thread.start()
        logger.info("消息监听已启动（轮询模式）")

    def _resolve_wechatauto_group_id(self, group: str) -> str | None:
        """将群名解析为群聊 wxid，避免同名联系人优先匹配。"""
        if group.endswith("@chatroom"):
            return group

        try:
            group_id = self._wx._db.group_name_to_id(group)
            if group_id:
                return group_id
        except Exception:
            pass

        try:
            for contact in self._wx._db.search_contact(group):
                username = contact.get("username", "")
                if username.endswith("@chatroom"):
                    return username
        except Exception:
            pass
        return None

    def _normalize_target_groups(self, target_groups: list[str]) -> list[str]:
        """去重并整理群聊名称，保持用户配置顺序。"""
        result: list[str] = []
        for group in target_groups or []:
            name = str(group or "").strip()
            if name and name not in result:
                result.append(name)
        return result

    async def set_target_groups(self, target_groups: list[str]) -> dict:
        """热更新需要监控的群聊列表，不重启微信连接。"""
        groups = self._normalize_target_groups(target_groups)
        self.target_groups = groups

        if (
            not self._wx
            or not self._running
            or self._status != ConnectionStatus.CONNECTED
        ):
            self._target_group_ids.clear()
            self._room_names_by_id.clear()
            self._listen_keys_by_group.clear()
            logger.info(
                f"监控群聊配置已更新（等待启动）: "
                f"{', '.join(groups) or '无'}"
            )
            return {
                "success": True,
                "live": False,
                "groups": groups,
                "registered": [],
                "unresolved": [],
                "message": "配置已保存，机器人启动后生效",
            }

        try:
            return await asyncio.to_thread(
                self._refresh_target_group_registration,
                groups,
            )
        except Exception as exc:
            logger.error(f"热更新监控群聊失败: {exc}")
            return {
                "success": False,
                "live": False,
                "groups": groups,
                "registered": [],
                "unresolved": [],
                "message": f"运行时更新失败，重启后生效: {exc}",
            }

    def _refresh_target_group_registration(
        self,
        groups: list[str],
    ) -> dict:
        """同步刷新群 wxid 映射，并在按群监听模式下补注册新群。"""
        previous_keys = set(self._listen_keys_by_group.values())
        resolved: list[tuple[str, str]] = []
        unresolved: list[str] = []

        for group in groups:
            group_id = group
            if self._backend == "wechatauto":
                group_id = self._resolve_wechatauto_group_id(group) or ""
                if not group_id:
                    unresolved.append(group)
                    logger.warning(
                        f"未在微信数据库中群聊 [{group}]，"
                        "本次暂未注册，启动或重连后会继续尝试"
                    )
                    continue
            resolved.append((group, group_id))

        self._room_names_by_id = {
            group_id: group for group, group_id in resolved
        }
        self._listen_keys_by_group = {
            group: group_id for group, group_id in resolved
        }
        self._target_group_ids = {
            group_id for _, group_id in resolved
        }

        if not self._listen_all_active:
            for group, listen_key in resolved:
                if listen_key in previous_keys:
                    continue
                try:
                    if self._backend in {"wxauto4", "wechatauto"}:
                        self._wx.AddListenChat(
                            listen_key,
                            callback=self._handle_callback_message,
                        )
                    else:
                        self._wx.AddListenChat(who=group)
                    self._registered_listen_keys_by_group[
                        group
                    ] = listen_key
                    logger.info(f"已新增监听群聊: {group} ({listen_key})")
                except Exception as exc:
                    logger.warning(f"新增监听群聊失败 [{group}]: {exc}")

        registered = [group for group, _ in resolved]
        logger.info(
            "监控群聊已热更新: "
            f"{', '.join(registered) or '无'}"
            + (
                f"；未找到: {', '.join(unresolved)}"
                if unresolved
                else ""
            )
        )
        return {
            "success": True,
            "live": True,
            "groups": groups,
            "registered": registered,
            "unresolved": unresolved,
            "message": (
                f"已实时更新 {len(registered)} 个群聊"
                + (
                    f"，{len(unresolved)} 个群名暂未找到"
                    if unresolved
                    else ""
                )
            ),
        }

    def _handle_callback_message(self, msg, chat) -> None:
        """接收 4.x 自动化库的回调消息。"""
        chat_id = (
            getattr(chat, "_wxid", None)
            or getattr(chat, "who", None)
            or str(chat)
        )
        chat_name = self._room_names_by_id.get(str(chat_id))
        if not chat_name:
            chat_name = self._chat_display_name(str(chat_id))
        self._handle_message(str(chat_id), msg, room_name=chat_name)

    def _poll_loop(self) -> None:
        """消息轮询循环（运行在独立线程）"""
        while self._running and self._wx:
            try:
                msgs = self._wx.GetListenMessage()
                if msgs:
                    for chat, messages in msgs.items():
                        chat_name = getattr(chat, "name", str(chat))
                        for msg in messages:
                            if msg.type == "friend":
                                self._handle_message(chat_name, msg)
            except Exception as e:
                if self._running:
                    logger.error(f"消息轮询异常: {e}")
                    time.sleep(2)
            time.sleep(1)

    def _handle_message(
        self,
        chat_id: str,
        msg,
        room_name: str | None = None,
    ) -> None:
        """处理单条消息"""
        try:
            chat_id = str(chat_id)
            is_group = chat_id.endswith("@chatroom")
            lower_id = chat_id.lower()
            if not is_group and (
                lower_id in self.IGNORED_PRIVATE_ACCOUNTS
                or lower_id.startswith("gh_")
            ):
                logger.debug(f"已忽略公众号/系统账号消息: {chat_id}")
                return

            room_name = room_name or self._chat_display_name(chat_id)
            raw_content = getattr(msg, "content", "") or ""
            if not isinstance(raw_content, str):
                raw_content = str(raw_content)
            sender = str(getattr(msg, "sender", "") or "").strip()
            if sender.startswith("wxid_"):
                # 把群成员/好友 wxid 换成昵称，日志和上下文更好读
                sender = self._chat_display_name(sender)
            elif not sender or (not is_group and sender == chat_id):
                sender = room_name
            content = self.normalize_message_content(
                raw_content,
                sender=sender,
            )
            is_self = self._is_self_message(
                msg,
                chat_id=chat_id,
                room_name=room_name,
                sender=sender,
                content=content,
            )
            if is_self:
                logger.debug(
                    f"已忽略机器人自己发出的消息 [{room_name}]: "
                    f"{content[:50]}"
                )
                return

            is_poke_me = False
            poke_sender = ""
            is_poke_me, poke_sender = self._detect_poke(raw_content)
            if is_poke_me:
                if not content:
                    content = f"{poke_sender or '有人'} 拍了拍本鲸鱼娘"
                # 拍一拍消息的原始发送者是群号/会话号，改成发起人，
                # 否则会被误判成机器人自己发的消息而丢弃
                if poke_sender:
                    sender = poke_sender
            local_id = getattr(msg, "local_id", None)
            sort_seq = getattr(msg, "sort_seq", None)
            raw_id = getattr(msg, "id", None)
            message_id = ":".join(
                str(part)
                for part in (chat_id, local_id or raw_id, sort_seq or "")
                if part
            )
            message_type = str(getattr(msg, "type", "text") or "text")
            voice_path = ""
            if message_type == "voice":
                voice_path = self._extract_voice_file(msg, chat_id)

            # 同一实例/多实例重复投递的消息只处理一次
            if message_id and self._is_duplicate_delivery(message_id):
                logger.debug(f"重复投递的消息已忽略: {message_id}")
                return

            message = WeChatMessage(
                id=message_id or str(time.time()),
                sender=sender,
                sender_name=sender,
                room_id=chat_id,
                room_name=room_name,
                content=content,
                type=3 if message_type == "image" else 1,
                timestamp=time.time(),
                is_group=is_group,
                is_at_me=self._is_at_me(msg, content),
                message_type=message_type,
                is_private=not is_group,
                is_target_group=(chat_id in self._target_group_ids)
                if is_group
                else True,
                is_poke_me=is_poke_me,
                is_self=is_self,
                poke_sender=poke_sender,
                voice_path=voice_path,
            )

            scope = "群聊" if is_group else "私聊"
            flag = "（拍一拍）" if is_poke_me else ""
            logger.info(
                f"[{scope}][{room_name}] {message.sender}: "
                f"{content[:50]}...{flag}"
            )

            if self.on_message and self._loop:
                asyncio.run_coroutine_threadsafe(
                    self.on_message(message), self._loop
                )

        except Exception as e:
            logger.error(f"消息处理异常: {e}")

    def _extract_voice_file(self, msg, chat_id: str) -> str:
        """Extract a wechatauto voice message from the local media database."""
        if self._backend != "wechatauto":
            logger.debug(f"[微信] {self._backend} 后端暂不提取语音文件")
            return ""

        local_id = getattr(msg, "local_id", None)
        chat = getattr(msg, "parent", None)
        root = getattr(chat, "root", None)
        db = getattr(root, "_db", None) or getattr(chat, "_db", None)
        if local_id is None or db is None:
            logger.debug(f"[微信] 语音消息缺少媒体上下文: {chat_id}")
            return ""

        user = str(
            getattr(root, "_wxid", "")
            or getattr(chat, "_wxid", "")
            or getattr(msg, "wxid", "")
            or chat_id
        ).strip()
        if not user:
            return ""

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        save_dir = os.path.join(root, "data", "voice_inbox")
        try:
            from wechatauto.media import MediaDownloader

            path = MediaDownloader(db, save_dir=save_dir).download_voice(
                user,
                int(local_id),
                save_dir=save_dir,
            )
            if path:
                logger.info(f"[微信] 已提取语音文件: {path}")
                return str(path)
            logger.warning(
                f"[微信] 本地媒体库未找到语音文件: {chat_id}/{local_id}"
            )
        except Exception as exc:
            logger.warning(f"[微信] 提取语音文件失败: {exc}")
        return ""

    @staticmethod
    def _identity_key(value: str) -> str:
        """归一化微信显示名，便于稳定比较机器人身份。"""
        text = html.unescape(str(value or "")).strip().casefold()
        return re.sub(r"[\s\u2000-\u200f\u2028-\u202f\u205f\u3000]+", "", text)

    def _self_identity_keys(self) -> set[str]:
        values = set(self._bot_names)
        if self._self_wxid:
            values.add(self._self_wxid)
        return {
            key
            for value in values
            if (key := self._identity_key(value))
        }

    def _is_self_message(
        self,
        msg,
        *,
        chat_id: str = "",
        room_name: str = "",
        sender: str = "",
        content: str = "",
    ) -> bool:
        """多路判断机器人自己的消息，避免回复回显形成自娱自乐循环。"""
        if bool(getattr(msg, "is_self", False)):
            return True
        try:
            if int(getattr(msg, "wxid", 0) or 0) == 2:
                return True
        except (TypeError, ValueError):
            pass
        raw_wxid = str(getattr(msg, "wxid", "") or "").strip()
        if (
            raw_wxid
            and self._self_wxid
            and raw_wxid == str(self._self_wxid).strip()
        ):
            return True
        if str(getattr(msg, "attr", "") or "").casefold() == "self":
            return True

        sender_key = self._identity_key(sender)
        if sender_key and sender_key in self._self_identity_keys():
            return True

        self_wxid = self._identity_key(self._self_wxid)
        if self_wxid and self._identity_key(chat_id) == self_wxid:
            return True

        # 微信 4.x 的监听回调偶尔丢失 attr/is_self，机器人刚发出的内容
        # 很快又会作为新消息回调回来。短时间内容指纹是最可靠兜底。
        return bool(
            content
            and room_name
            and self._was_recently_sent(
                room_name,
                content,
                max_age=15.0,
            )
        )

    def _is_duplicate_delivery(self, message_id: str) -> bool:
        """同一消息被重复投递（多监听/多实例）时只处理一次。"""
        now = time.monotonic()
        if len(self._seen_message_ids) > 2000:
            self._seen_message_ids = {
                key: stamp
                for key, stamp in self._seen_message_ids.items()
                if now - stamp < self.MESSAGE_DEDUPE_WINDOW_SECONDS
            }
        if message_id in self._seen_message_ids:
            return True
        self._seen_message_ids[message_id] = now
        return False

    def _chat_display_name(self, chat_id: str) -> str:
        """把会话 wxid 解析成可搜索的显示名（群名/联系人昵称）。"""
        cached = self._room_names_by_id.get(chat_id) or self._chat_names.get(chat_id)
        if cached:
            return cached

        name = chat_id
        try:
            nickname = self._wx._db.get_nickname(chat_id)
            if nickname:
                name = str(nickname).strip() or chat_id
        except Exception:
            pass
        if chat_id == "filehelper":
            name = "文件传输助手"
        self._chat_names[chat_id] = name
        return name

    def _detect_poke(self, raw_content: str) -> tuple[bool, str]:
        """识别拍一拍消息，返回 (是否拍了机器人, 发起人显示名)。

        微信 4.x 的拍一拍是 appmsg(type=62)，字段在 ``<patinfo>`` 里：
        ``pattedusername`` 是被拍的人，``fromusername`` 是发起人。
        """
        text = raw_content or ""
        if "<patinfo>" not in text:
            return False, ""

        patinfo = text[text.index("<patinfo>"):]
        patted = re.search(
            r"<pattedusername>(.*?)</pattedusername>",
            patinfo,
            flags=re.DOTALL,
        )
        patted_username = patted.group(1).strip() if patted else ""
        if self._self_wxid and patted_username:
            if patted_username != self._self_wxid:
                return False, ""
        elif not patted_username:
            title = re.search(r"<title>(.*?)</title>", text, flags=re.DOTALL)
            title_text = title.group(1) if title else ""
            if not self._poke_targets_me(title_text):
                return False, ""

        poker = ""
        source = re.search(
            r"<fromusername>(.*?)</fromusername>",
            patinfo,
            flags=re.DOTALL,
        )
        if source:
            poker_wxid = source.group(1).strip()
            if poker_wxid:
                poker = self._chat_display_name(poker_wxid)
        if not poker:
            title = re.search(r"<title>(.*?)</title>", text, flags=re.DOTALL)
            if title:
                matched = re.match(
                    r'\s*"?([^"<>]{1,40}?)"?\s*(?:拍了拍|戳了戳|碰了碰|敲了敲)',
                    title.group(1),
                )
                if matched:
                    poker = matched.group(1).strip()
        return True, poker

    def _poke_targets_me(self, title_text: str) -> bool:
        """在缺少 pattedusername 时，用标题里的被拍名称兜底判断。"""
        if not title_text:
            return False
        names = set(self._bot_names)
        names.add("鲸鱼娘")
        for name in names:
            if not name:
                continue
            pattern = (
                rf"(?:拍了拍|戳了戳|碰了碰|敲了敲)\s*[\"“]?{re.escape(name)}"
            )
            if re.search(pattern, title_text):
                return True
        return False

    @staticmethod
    def normalize_message_content(content: str, sender: str = "") -> str:
        """把微信卡片、引用和表情压缩成适合上下文使用的短文本。"""
        text = html.unescape(content or "").strip()
        if not text:
            return ""

        if sender:
            prefix = f"{sender.strip()}:"
            if text.startswith(prefix):
                text = text[len(prefix):].lstrip()
        text = re.sub(r"^wxid_[A-Za-z0-9_-]{4,}\s*:\s*", "", text)

        if text.startswith("<msg"):
            if "<emoji" in text:
                return "[表情]"
            if "<img" in text:
                return "[图片]"

            title_match = re.search(
                r"<title\b[^>]*>(.*?)</title>",
                text,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if title_match:
                title = re.sub(
                    r"<!\[CDATA\[(.*?)\]\]>",
                    r"\1",
                    title_match.group(1),
                    flags=re.DOTALL,
                ).strip()
                title = re.sub(r"\s+", " ", title)
                if title:
                    return title[:300]

            desc_match = re.search(
                r"<des\b[^>]*>(.*?)</des>",
                text,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if desc_match:
                desc = re.sub(r"\s+", " ", desc_match.group(1)).strip()
                if desc:
                    return desc[:300]
            return "[卡片消息]"

        return text.strip()

    def _is_at_me(self, msg, content: str) -> bool:
        """判断消息是否明确 @ 当前机器人。"""
        if bool(getattr(msg, "is_at", False)):
            return True
        if bool(getattr(msg, "is_at_me", False)):
            return True
        if not self._bot_names:
            return False

        for name in self._bot_names:
            pattern = (
                rf"@{re.escape(name)}"
                r"(?=$|[\s\u2000-\u200f\u2028-\u202f\u205f\u3000，,。.!！？:：])"
            )
            if re.search(pattern, content or ""):
                return True
        return False

    async def send_text(self, room_name: str, content: str) -> bool:
        """发送文本消息（带节流、冷却与单线程隔离，避免微信界面卡死拖垮机器人）"""
        if not self._wx or self._status != ConnectionStatus.CONNECTED:
            logger.error("微信未连接，无法发送消息")
            return False

        if self._was_recently_sent(room_name, content):
            logger.info(f"消息已在超时重试前成功发出，跳过重复发送: [{room_name}]")
            return True

        if self._send_is_inflight(room_name, content):
            logger.warning(
                "相同内容仍有发送任务在处理，等待结果以避免重复: "
                f"[{room_name}]"
            )
            deadline = time.monotonic() + self.SEND_BACKOFF_MAX_SECONDS
            while time.monotonic() < deadline:
                if self._was_recently_sent(room_name, content):
                    return True
                if not self._send_is_inflight(room_name, content):
                    break
                await asyncio.sleep(0.2)
            if self._was_recently_sent(room_name, content):
                return True
            if self._send_is_inflight(room_name, content):
                logger.warning(
                    "发送结果尚未确认，为避免重复已跳过同内容再次发送: "
                    f"[{room_name}]"
                )
                return True

        now = time.monotonic()
        if now < self._send_backoff_until:
            wait = min(
                self.SEND_BACKOFF_MAX_SECONDS,
                max(0.0, self._send_backoff_until - now),
            )
            logger.warning(
                f"发送短冷却中（还剩约 {wait:.1f} 秒），"
                f"等待恢复后重试: [{room_name}]"
            )
            await asyncio.sleep(wait)

        if self._sender_busy.is_set():
            logger.warning(
                "上一次发送仍未返回，先等待发送线程恢复，再尝试本次发送: "
                f"[{room_name}]"
            )
            idle = await self._wait_sender_idle(
                timeout=self.SEND_BACKOFF_MAX_SECONDS,
            )
            if not idle:
                self._note_send_failure("发送线程持续未返回")
                return False

        try:
            async with self._get_send_gate():
                wait = (
                    self._last_send_at
                    + self.SEND_MIN_INTERVAL_SECONDS
                    - time.monotonic()
                )
                if wait > 0:
                    await asyncio.sleep(wait + random.uniform(0.0, 0.4))
                if self.reply_delay > 0:
                    await asyncio.sleep(self.reply_delay)

                success = await asyncio.wait_for(
                    self._enqueue_send(room_name, content),
                    timeout=self.SEND_TIMEOUT_SECONDS,
                )
        except asyncio.TimeoutError:
            logger.error(
                f"发送消息超时（超过 {self.SEND_TIMEOUT_SECONDS:.1f} 秒），"
                f"已跳过本次卡住的发送: [{room_name}]"
            )
            self._note_send_failure("发送超时")
            return False
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            self._note_send_failure(str(e))
            return False

        if not success:
            logger.error(f"发送消息失败: [{room_name}]")
            self._note_send_failure("发送接口返回失败")
            return False

        self._last_send_at = time.monotonic()
        self._send_failures = 0
        self._send_backoff_until = 0.0
        self._remember_sent(room_name, content)
        logger.info(f"消息已发送到 [{room_name}]: {content[:50]}...")
        return True

    async def _wait_sender_idle(self, timeout: float) -> bool:
        """短暂等待串行发送线程恢复，避免一次超时永久挡住后续消息。"""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while self._sender_busy.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        return not self._sender_busy.is_set()

    def _sent_key(self, room_name: str, content: str) -> tuple[str, str]:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return room_name, digest

    def _remember_sent(self, room_name: str, content: str) -> None:
        now = time.monotonic()
        self._recent_sent[self._sent_key(room_name, content)] = now
        if len(self._recent_sent) > 500:
            cutoff = now - 60.0
            self._recent_sent = {
                key: stamp
                for key, stamp in self._recent_sent.items()
                if stamp >= cutoff
            }

    def _mark_send_inflight(self, room_name: str, content: str) -> bool:
        """标记内容正在发送，阻止超时后的同内容重复投递。"""
        key = self._sent_key(room_name, content)
        now = time.monotonic()
        with self._send_inflight_lock:
            if len(self._send_inflight) > 200:
                cutoff = now - self.SEND_ITEM_TTL_SECONDS
                self._send_inflight = {
                    item_key: stamp
                    for item_key, stamp in self._send_inflight.items()
                    if stamp >= cutoff
                }
            if key in self._send_inflight:
                return False
            self._send_inflight[key] = now
            return True

    def _clear_send_inflight(self, room_name: str, content: str) -> None:
        with self._send_inflight_lock:
            self._send_inflight.pop(
                self._sent_key(room_name, content),
                None,
            )

    def _send_is_inflight(self, room_name: str, content: str) -> bool:
        key = self._sent_key(room_name, content)
        with self._send_inflight_lock:
            stamp = self._send_inflight.get(key, 0.0)
            if not stamp:
                return False
            return (
                time.monotonic() - stamp
                <= self.SEND_ITEM_TTL_SECONDS
            )

    def _was_recently_sent(
        self,
        room_name: str,
        content: str,
        max_age: float = 30.0,
    ) -> bool:
        stamp = self._recent_sent.get(self._sent_key(room_name, content), 0.0)
        return bool(
            stamp
            and time.monotonic() - stamp <= max(0.0, float(max_age))
        )

    def _get_send_gate(self) -> asyncio.Lock:
        """惰性创建发送串行锁（避免在无事件循环时构造）。"""
        if self._send_gate is None:
            self._send_gate = asyncio.Lock()
        return self._send_gate

    async def _enqueue_send(self, room_name: str, content: str) -> bool:
        """把发送任务交给专用发送线程，并等待结果。"""
        if not self._accepting_sends:
            logger.debug(f"机器人正在停止，忽略发送任务: [{room_name}]")
            return False
        if not self._mark_send_inflight(room_name, content):
            logger.debug(f"同内容发送已在处理中，忽略重复入队: [{room_name}]")
            return True
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        item = (
            room_name,
            content,
            time.monotonic() + self.SEND_ITEM_TTL_SECONDS,
            loop,
            future,
        )
        try:
            self._send_queue.put_nowait(item)
        except queue.Full:
            self._clear_send_inflight(room_name, content)
            logger.warning(f"发送队列已满，丢弃本次发送: [{room_name}]")
            return False
        self._ensure_sender_thread()
        return await future

    def _ensure_sender_thread(self) -> None:
        if self._sender_thread and self._sender_thread.is_alive():
            return
        self._sender_thread = threading.Thread(
            target=self._sender_loop,
            args=(self._send_queue, self._sender_busy),
            daemon=True,
            name="wechat-sender",
        )
        self._sender_thread.start()

    def _sender_loop(
        self,
        send_queue: queue.Queue,
        busy_event: threading.Event,
    ) -> None:
        """专用发送线程：串行执行 UIA 发送，卡住时只影响后续发送。"""
        while True:
            item = send_queue.get()
            if item is None:
                return
            room_name, content, deadline, loop, future = item
            if time.monotonic() > deadline:
                self._clear_send_inflight(room_name, content)
                logger.warning(f"发送任务已过期，丢弃: [{room_name}]")
                self._set_send_result(loop, future, False)
                continue

            busy_event.set()
            if busy_event is self._sender_busy:
                self._sender_busy_since = time.monotonic()
            try:
                ok = bool(self._send_text_sync(room_name, content))
            except Exception as e:
                if busy_event is self._sender_busy:
                    logger.error(f"发送消息失败: {e}")
                else:
                    logger.debug(f"已隔离的旧发送线程结束: {e}")
                ok = False
            finally:
                busy_event.clear()
                if busy_event is self._sender_busy:
                    self._sender_busy_since = 0.0
            if ok:
                self._remember_sent(room_name, content)
            self._clear_send_inflight(room_name, content)
            self._set_send_result(loop, future, ok)

    @staticmethod
    def _set_send_result(loop, future, ok: bool) -> None:
        def _finish() -> None:
            if future.done():
                return
            future.set_result(ok)

        try:
            loop.call_soon_threadsafe(_finish)
        except Exception:
            pass

    def _note_send_failure(self, reason: str) -> None:
        """发送异常后进入短冷却，下一次回复会重新尝试。"""
        self._send_failures += 1
        backoff = min(
            self.SEND_BACKOFF_BASE_SECONDS * (2 ** (self._send_failures - 1)),
            self.SEND_BACKOFF_MAX_SECONDS,
        )
        self._send_backoff_until = time.monotonic() + backoff
        logger.warning(
            f"微信发送异常（{reason}），{backoff:.0f} 秒后恢复重试"
        )

    def _send_text_sync(self, room_name: str, content: str) -> bool:
        """在线程中执行同步发送，避免阻塞 asyncio 事件循环。"""
        if self._backend == "wechatauto":
            try:
                from wechatauto.utils.lock import LockManager

                with LockManager.acquire():
                    if self._send_text_with_uia(room_name, content):
                        return True
                    if not self.has_usable_uia():
                        logger.debug(
                            "微信 UIA 不可用，使用 OCR 兼容发送路径"
                        )
                    result = self._wx.SendMsg(
                        content,
                        who=room_name,
                        exact=True,
                    )
                    if not result:
                        logger.error(f"发送消息失败: {result}")
                        return False
                    return True
            except Exception as e:
                logger.error(f"发送消息失败: {e}")
                return False

        if self._backend == "wxauto4":
            result = self._wx.SendMsg(content, who=room_name, exact=True)
            if not result:
                logger.error(f"发送消息失败: {result}")
                return False
            return True

        self._wx.SendMsg(content, who=room_name)
        return True

    def _send_text_with_uia(self, room_name: str, content: str) -> bool:
        """优先走纯 UIA 输入发送，绕开截图检测导致的偶发卡死。"""
        if not self._uia_available:
            return False
        gui = getattr(self._wx, "_gui", None)
        if gui is None:
            return False

        uia = gui._get_uia()
        if uia is None:
            return False

        try:
            current_chat = uia.current_chat() or ""
            # Only use the fast path when the target is already open. Calling
            # send_text_to here performs a second full search/open flow when
            # the regular SendMsg path will do the same work and is faster.
            if not self._uia_chat_is_current(current_chat, room_name):
                return False
            if hasattr(uia, "ensure_window") and not uia.ensure_window():
                return False
            sent = bool(uia.send_text(content))
            if sent:
                logger.debug(f"已通过 UIA 快路径发送到 [{room_name}]")
            return sent
        except Exception as e:
            logger.debug(f"UIA 快路径发送失败，将回退完整发送流程: {e}")
            return False

    @staticmethod
    def _uia_chat_is_current(current_chat: str, room_name: str) -> bool:
        """识别带语音输入提示后缀的当前会话名。"""
        current_chat = current_chat or ""
        return (
            current_chat == room_name
            or current_chat.startswith(f"{room_name} ")
            or current_chat.startswith(f"{room_name}\u3000")
        )

    async def send_image(
        self,
        room_name: str,
        image_path: str | os.PathLike,
        timeout: float = 15.0,
    ) -> bool | None:
        """发送本地图片，复用发送串行锁，避免与文本发送抢占微信窗口。"""
        path = os.path.abspath(os.fspath(image_path))
        if not os.path.isfile(path):
            logger.error(f"图片文件不存在，无法发送: {path}")
            return False
        if not self._wx or self._status != ConnectionStatus.CONNECTED:
            logger.error("微信未连接，无法发送图片")
            return False

        send_gate = self._get_send_gate()
        gate_held = False
        send_task: asyncio.Task | None = None
        try:
            await send_gate.acquire()
            gate_held = True
            wait = (
                self._last_send_at
                + self.SEND_MIN_INTERVAL_SECONDS
                - time.monotonic()
            )
            if wait > 0:
                await asyncio.sleep(wait)
            send_task = asyncio.create_task(
                asyncio.to_thread(
                    self._send_image_sync,
                    room_name,
                    path,
                )
            )
            success = await asyncio.wait_for(
                asyncio.shield(send_task),
                timeout=max(5.0, float(timeout)),
            )
        except asyncio.TimeoutError:
            if send_task is not None:
                asyncio.create_task(
                    self._observe_image_send(
                        send_task,
                        room_name,
                        path,
                        send_gate,
                    )
                )
                # The observer owns the gate until the underlying sync send
                # finishes, so later text cannot race the image paste/send.
                gate_held = False
            logger.warning(
                "发送图片结果暂未确认，已跳过失败兜底，"
                f"后台继续等待: [{room_name}] {path}"
            )
            return None
        except Exception as exc:
            logger.error(f"发送图片失败: [{room_name}] {exc}")
            return False
        finally:
            if gate_held:
                send_gate.release()

        if not success:
            logger.error(f"发送图片失败: [{room_name}] {path}")
            return False
        self._last_send_at = time.monotonic()
        logger.info(f"图片已发送到 [{room_name}]: {path}")
        return True

    async def _observe_image_send(
        self,
        task: asyncio.Task,
        room_name: str,
        image_path: str,
        send_gate: asyncio.Lock | None = None,
    ) -> None:
        """确认超时图片的最终结果，避免后台成功后又补发失败文案。"""
        success = False
        try:
            success = bool(await task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                f"后台图片发送确认失败: [{room_name}] {image_path}: {exc}"
            )
            return
        finally:
            if send_gate is not None and send_gate.locked():
                send_gate.release()
            if success:
                self._last_send_at = time.monotonic()
                logger.info(
                    "图片已在后台确认发送成功，"
                    f"跳过失败文案: [{room_name}] {image_path}"
                )
            else:
                logger.error(
                    f"图片后台确认发送失败: [{room_name}] {image_path}"
                )

    def _send_image_sync(self, room_name: str, image_path: str) -> bool:
        """Send through clipboard paste first, then fall back to SendFiles()."""
        if self._backend == "wechatauto":
            try:
                from wechatauto.utils.lock import LockManager

                with LockManager.acquire():
                    gui = getattr(self._wx, "_gui", None)
                    send_image = getattr(gui, "send_image", None)
                    if callable(send_image):
                        try:
                            result = send_image(
                                image_path,
                                who=room_name,
                                verify=False,
                            )
                            if result is None or bool(result):
                                logger.info(
                                    "图片已通过剪贴板粘贴发送: "
                                    f"[{room_name}] {image_path}"
                                )
                                return True
                            logger.warning(
                                "剪贴板粘贴发送图片失败，回退 SendFiles: "
                                f"[{room_name}] {result}"
                            )
                        except Exception as exc:
                            logger.warning(
                                "剪贴板粘贴发送图片异常，回退 SendFiles: "
                                f"[{room_name}] {exc}"
                            )

                    result = self._wx.SendFiles(
                        image_path,
                        who=room_name,
                        exact=True,
                    )
                    return result is None or bool(result)
            except Exception as exc:
                logger.error(f"发送图片失败: {exc}")
                return False

        if self._backend == "wxauto4":
            result = self._wx.SendFiles(image_path, who=room_name)
            return result is None or bool(result)

        result = self._wx.SendFiles(image_path, who=room_name)
        return result is None or bool(result)

    async def send_file(
        self,
        room_name: str,
        file_path: str | os.PathLike,
        timeout: float = 45.0,
    ) -> bool:
        """发送本地文件（语音 / 歌曲等），复用发送串行锁避免与文本抢窗口。"""
        path = os.path.abspath(os.fspath(file_path))
        if not os.path.isfile(path):
            logger.error(f"文件不存在，无法发送: {path}")
            return False
        if not self._wx or self._status != ConnectionStatus.CONNECTED:
            logger.error("微信未连接，无法发送文件")
            return False

        send_gate = self._get_send_gate()
        gate_held = False
        try:
            await send_gate.acquire()
            gate_held = True
            wait = (
                self._last_send_at
                + self.SEND_MIN_INTERVAL_SECONDS
                - time.monotonic()
            )
            if wait > 0:
                await asyncio.sleep(wait)
            success = await asyncio.wait_for(
                asyncio.to_thread(self._send_file_sync, room_name, path),
                timeout=max(5.0, float(timeout)),
            )
        except asyncio.TimeoutError:
            logger.warning(f"发送文件超时: [{room_name}] {path}")
            return False
        except Exception as exc:
            logger.error(f"发送文件失败: [{room_name}] {exc}")
            return False
        finally:
            if gate_held:
                send_gate.release()

        if not success:
            logger.error(f"发送文件失败: [{room_name}] {path}")
            return False
        self._last_send_at = time.monotonic()
        logger.info(f"文件已发送到 [{room_name}]: {path}")
        return True

    def _send_file_sync(self, room_name: str, file_path: str) -> bool:
        """同步发送文件，统一走 wxauto 的 SendFiles。"""
        try:
            if self._backend == "wechatauto":
                from wechatauto.utils.lock import LockManager

                with LockManager.acquire():
                    result = self._wx.SendFiles(
                        file_path,
                        who=room_name,
                        exact=True,
                    )
                    return result is None or bool(result)
            result = self._wx.SendFiles(file_path, who=room_name)
            return result is None or bool(result)
        except Exception as exc:
            logger.error(f"发送文件异常: {exc}")
            return False

    async def send_random_favorite_sticker(
        self,
        room_name: str,
        preferred_only: bool = False,
        limit: int = 3,
        is_group: bool = False,
    ) -> bool:
        """从微信收藏表情中随机点击一个表情，发到「当前打开的会话」。

        不做跨窗口切换：目标不是当前聊天窗口时直接返回 False。
        is_group 仅为 API 兼容保留，不影响发送逻辑。
        """
        if not self._wx or self._status != ConnectionStatus.CONNECTED:
            logger.error("微信未连接，无法发送收藏表情")
            return False
        if self._backend != "wechatauto":
            logger.warning("当前后端不支持收藏表情 UIA 发送，仅 wechatauto 可用")
            return False
        if not self._uia_available:
            logger.debug("当前微信窗口为 OCR 兼容模式，暂无 UIA 收藏表情入口")
            return False
        if not getattr(self._wx, "_gui", None):
            logger.warning("微信 GUI 对象不可用，无法发送收藏表情")
            return False

        return await asyncio.to_thread(
            self._send_random_favorite_sticker_sync,
            room_name,
            preferred_only,
            limit,
            is_group,
        )

    def _send_random_favorite_sticker_sync(
        self,
        room_name: str,
        preferred_only: bool = False,
        limit: int = 3,
        is_group: bool = False,
    ) -> bool:
        """在线程中执行 UIA 点击，调用方负责等待。

        is_group 仅为 API 兼容保留；收藏表情只在当前打开的会话发送，
        目标不是当前窗口时跳过、不切换窗口。
        """
        try:
            from wechatauto.utils.lock import LockManager
        except Exception as e:
            logger.error(f"加载微信自动化锁失败: {e}")
            return False

        with LockManager.acquire():
            gui = self._wx._gui
            try:
                if not gui.ensure_visible():
                    logger.warning("微信窗口不可见，无法发送收藏表情")
                    return False

                uia = gui._get_uia()
                if uia is None:
                    logger.warning("微信 UIA 不可用，无法定位收藏表情")
                    return False
                if hasattr(uia, "ensure_window") and not uia.ensure_window():
                    logger.warning("微信 UIA 窗口未就绪，无法发送收藏表情")
                    return False
                # 收藏表情只在「当前已打开的聊天窗口」发送，不通过 open_chat
                # 跳转到其他窗口（微信风控会把跨窗口跳转识别为风险行为而拒绝）。
                # 目标不是当前会话就直接跳过，绝不切窗口。
                current_chat = uia.current_chat() or ""
                if not self._uia_chat_is_current(current_chat, room_name):
                    logger.debug(
                        f"目标会话 [{room_name}] 不是当前打开的窗口，"
                        f"跳过发送收藏表情（不切换窗口）"
                    )
                    return False

                window = getattr(uia, "_win", None)
                if window is None:
                    logger.warning("UIA 窗口句柄不存在，无法发送收藏表情")
                    return False

                launcher = self._find_sticker_launcher(window)
                if launcher is None:
                    logger.warning("未找到微信输入框的表情按钮")
                    return False
                panel = self._find_sticker_panel(window)
                if panel is None:
                    if not self._click_uia_control(uia, launcher):
                        logger.warning("点击微信表情按钮失败")
                        return False

                    time.sleep(0.8)
                    panel = self._find_sticker_panel(window)
                    if panel is None:
                        logger.warning("表情面板未打开或未被 UIA 暴露")
                        return False

                items = self._find_favorite_items(
                    panel,
                    include_generic=False,
                )
                clicked_favorite_tab = False
                if not items:
                    favorite_tab = self._find_favorite_tab(panel)
                    if favorite_tab is not None:
                        clicked_favorite_tab = self._click_uia_control(
                            uia,
                            favorite_tab,
                        )
                        if clicked_favorite_tab:
                            time.sleep(0.8)
                            panel = self._find_sticker_panel(window) or panel
                            items = self._find_favorite_items(
                                panel,
                                include_generic=True,
                            )
                if not items:
                    logger.warning("收藏表情面板中没有可点击的可见表情")
                    return False

                items.sort(key=_control_bounds)
                if preferred_only:
                    items = items[:max(1, int(limit))]
                chosen = random.choice(items)
                if not self._click_uia_control(uia, chosen):
                    logger.warning("点击收藏表情失败")
                    return False

                time.sleep(0.5)
                logger.info(f"已从收藏表情中随机发送一个表情到 [{room_name}]")
                return True
            except Exception as e:
                logger.error(f"发送收藏表情失败: {e}")
                return False

    @staticmethod
    def _find_sticker_launcher(window):
        """在聊天输入工具栏中定位表情入口按钮。"""
        blocked_classes = (
            "emoticonpanel",
            "emoticongridview",
            "emoticoncontentview",
            "favemoticonitemview",
            "expressioncollectitem",
            "emoticontoolbaritem",
        )
        toolbar_classes = (
            "chatinputtoolbarleftview",
            "chatinputtoolbarrightview",
            "chatinputview",
        )

        def is_launcher(control, allow_class_match: bool = True) -> bool:
            control_class = (getattr(control, "ClassName", "") or "").lower()
            automation_id = (getattr(control, "AutomationId", "") or "").lower()
            name = (getattr(control, "Name", "") or "").strip()
            if any(token in control_class for token in blocked_classes):
                return False
            if _control_bounds(control) is None:
                return False

            haystack = f"{control_class} {automation_id} {name}".lower()
            if "发送表情" in name or name in {"表情", "表情包"}:
                return True
            if "chat_text_emoticon_view" in haystack:
                return True
            return allow_class_match and control_class == "mmui::emoticonview"

        roots = _find_uia_controls(
            window,
            lambda control: any(
                token in (getattr(control, "ClassName", "") or "").lower()
                for token in toolbar_classes
            ),
            max_depth=30,
        )
        roots.append(window)

        for root in roots:
            matches = _find_uia_controls(root, is_launcher, max_depth=16)
            if matches:
                matches.sort(key=lambda control: _control_bounds(control))
                return matches[0]

        exact_matches = _find_uia_controls(
            window,
            lambda control: is_launcher(control, allow_class_match=False),
            max_depth=30,
        )
        if exact_matches:
            exact_matches.sort(key=lambda control: _control_bounds(control))
            return exact_matches[0]
        return None

    @staticmethod
    def _find_sticker_panel(window):
        """定位当前展开的微信表情面板或弹窗。"""
        matches = _find_uia_controls(
            window,
            lambda control: (
                (getattr(control, "ClassName", "") or "").lower()
                in {"mmui::emoticonpanel", "mmui::emoticonpopover"}
                or "emoticon_panel" in (
                    getattr(control, "AutomationId", "") or ""
                ).lower()
            )
            and _control_bounds(control) is not None,
            max_depth=30,
        )
        if matches:
            return matches[0]

        try:
            import uiautomation as auto

            for popup in auto.GetRootControl().GetChildren():
                class_name = (getattr(popup, "ClassName", "") or "").lower()
                if class_name in {
                    "mmui::emoticonpopover",
                    "mmui::emoticonpanel",
                }:
                    return popup
        except Exception:
            pass
        return None

    @staticmethod
    def _find_favorite_tab(panel):
        """定位表情面板中的收藏标签。"""
        def is_favorite_tab(control) -> bool:
            control_class = (getattr(control, "ClassName", "") or "").lower()
            if control_class != "mmui::emoticontoolbaritem":
                return False
            if _control_bounds(control) is None:
                return False
            haystack = (
                f"{getattr(control, 'Name', '') or ''} "
                f"{getattr(control, 'AutomationId', '') or ''}"
            ).lower()
            return (
                "收藏" in haystack
                or "自定义表情" in haystack
                or "fav" in haystack
                or "favorite" in haystack
            )

        matches = _find_uia_controls(panel, is_favorite_tab, max_depth=12)
        return matches[0] if matches else None

    @staticmethod
    def _find_favorite_items(panel, include_generic: bool = False) -> list:
        """收集收藏表情面板内可见、尺寸正常的表情项。"""
        def is_favorite_item(control) -> bool:
            control_class = (getattr(control, "ClassName", "") or "").lower()
            automation_id = (getattr(control, "AutomationId", "") or "").lower()
            if not (
                control_class == "mmui::favemoticonitemview"
                or "favemoticon" in automation_id
                or (
                    include_generic
                    and control_class in {"mmui::emoticonitemview", "mmui::commonemoticonview"}
                )
            ):
                return False

            bounds = _control_bounds(control)
            if bounds is None:
                return False
            _, _, right, bottom = bounds
            left, top, _, _ = bounds
            width, height = right - left, bottom - top
            return 18 <= width <= 220 and 18 <= height <= 220

        return _find_uia_controls(panel, is_favorite_item, max_depth=24)

    @staticmethod
    def _click_uia_control(uia, control) -> bool:
        """优先使用 wechatauto 的真实鼠标点击，失败才调用控件默认点击。"""
        try:
            if hasattr(uia, "_click_ctrl"):
                return bool(uia._click_ctrl(control))
        except Exception:
            pass
        try:
            control.Click()
            return True
        except Exception:
            return False

    async def disconnect(self) -> None:
        """断开微信连接（带超时，避免停止机器人时卡死）"""
        self._running = False
        self._accepting_sends = False
        self._drain_send_queue()
        if self._sender_busy.is_set():
            if not await self._wait_sender_idle(timeout=4.0):
                self.abandon_stuck_sender()
        wx = self._wx
        if wx:
            def _teardown() -> None:
                if self._listen_all_active:
                    try:
                        wx.RemoveListenAll()
                    except Exception:
                        pass
                if self._backend in {"wxauto4", "wechatauto"}:
                    try:
                        wx.StopListening(remove=False)
                    except Exception:
                        pass
                registered_groups = (
                    self._registered_listen_keys_by_group
                    or self._listen_keys_by_group
                )
                for group, listen_key in registered_groups.items():
                    try:
                        if self._backend in {"wxauto4", "wechatauto"}:
                            wx.RemoveListenChat(
                                listen_key,
                                close_window=False,
                            )
                        else:
                            wx.RemoveListenChat(who=group)
                    except Exception:
                        pass
                self._registered_listen_keys_by_group.clear()

            try:
                await asyncio.wait_for(
                    asyncio.to_thread(_teardown),
                    timeout=12.0,
                )
            except asyncio.TimeoutError:
                logger.warning("断开微信监听超时，已放弃等待（不影响重新启动）")
            except Exception as e:
                logger.warning(f"断开微信监听异常: {e}")
        self._listen_all_active = False
        self._status = ConnectionStatus.DISCONNECTED
        logger.info("微信连接已断开")

    def _drain_send_queue(self) -> None:
        """Resolve queued sends as cancelled while the bot is stopping."""
        while True:
            try:
                item = self._send_queue.get_nowait()
            except queue.Empty:
                return
            if item is None:
                continue
            try:
                _, room_name, _, loop, future = item
                self._set_send_result(loop, future, False)
                logger.debug(f"停止时取消排队消息: [{room_name}]")
            except Exception:
                continue

    @property
    def status_info(self) -> dict:
        return {
            "status": self._status.value,
            "target_groups": self.target_groups,
            "listen_private": self.listen_private,
        }
