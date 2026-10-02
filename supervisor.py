"""微信机器人看门狗：崩溃检测、自动重启、日志保留。"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from src.error_logger import (
    log_error_message,
    log_exception,
    mark_startup,
)


ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
RUNTIME_DIR = ROOT / "runtime"
PYTHON_EXE = ROOT / ".venv" / "Scripts" / "python.exe"
CHILD_ENTRY = ROOT / "main.py"

SUPERVISOR_LOG = LOG_DIR / "supervisor.log"
CONSOLE_LOG = LOG_DIR / "console.log"
HEARTBEAT_FILE = RUNTIME_DIR / "bot.heartbeat"
CLEAN_EXIT_FILE = RUNTIME_DIR / "bot.clean-exit"
STOP_FILE = RUNTIME_DIR / "supervisor.stop"

HEALTH_INTERVAL_SECONDS = 3.0
STARTUP_GRACE_SECONDS = 75.0
HEARTBEAT_STALE_SECONDS = 60.0
STOP_WAIT_SECONDS = 12.0
BRIDGE_PORT = 47653
SUPERVISOR_LOCK_PORT = 47654
RESTART_DELAYS = (2.0, 3.0, 5.0, 10.0, 20.0, 30.0)


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", errors="replace") as handle:
        handle.write(text)
        if not text.endswith("\n"):
            handle.write("\n")


def log(message: str, level: str = "INFO") -> None:
    line = f"{_timestamp()} | {level:<7} | supervisor | {message}"
    _append_text(SUPERVISOR_LOG, line)
    print(line, flush=True)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log(f"清理运行时文件失败 {path}: {exc}", "WARNING")


def _heartbeat_age() -> float | None:
    try:
        return max(0.0, time.time() - HEARTBEAT_FILE.stat().st_mtime)
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _port_is_listening(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        return True
    else:
        return False
    finally:
        try:
            probe.close()
        except OSError:
            pass


class BotSupervisor:
    """在同一个管理员会话中运行并重启机器人。"""

    def __init__(self) -> None:
        self._child: subprocess.Popen[str] | None = None
        self._child_lock = threading.Lock()
        self._stopping = False
        self._consecutive_failures = 0
        self._instance_lock: socket.socket | None = None

    def _acquire_instance_lock(self) -> bool:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", SUPERVISOR_LOCK_PORT))
            probe.listen(1)
        except OSError:
            try:
                probe.close()
            except OSError:
                pass
            return False
        self._instance_lock = probe
        return True

    def _release_instance_lock(self) -> None:
        lock = self._instance_lock
        self._instance_lock = None
        if lock is None:
            return
        try:
            lock.close()
        except OSError:
            pass

    def request_stop(self, reason: str) -> None:
        if self._stopping:
            return
        self._stopping = True
        log(f"收到停止请求：{reason}", "WARNING")

    def _forward_output(self, stream) -> None:
        try:
            for raw_line in iter(stream.readline, ""):
                line = raw_line.rstrip("\r\n")
                if not line:
                    continue
                _append_text(CONSOLE_LOG, f"{_timestamp()} | {line}")
                print(line, flush=True)
        except Exception as exc:
            log_exception("supervisor._forward_output", exc=exc)
            log(f"读取机器人输出失败：{exc}", "WARNING")
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _start_child(self) -> subprocess.Popen[str]:
        if not PYTHON_EXE.is_file():
            raise FileNotFoundError(f"找不到虚拟环境 Python：{PYTHON_EXE}")
        if not CHILD_ENTRY.is_file():
            raise FileNotFoundError(f"找不到机器人入口：{CHILD_ENTRY}")

        _remove_file(CLEAN_EXIT_FILE)
        _remove_file(HEARTBEAT_FILE)

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        child = subprocess.Popen(
            [str(PYTHON_EXE), str(CHILD_ENTRY)],
            cwd=str(ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        with self._child_lock:
            self._child = child

        reader = threading.Thread(
            target=self._forward_output,
            args=(child.stdout,),
            name="bot-output",
            daemon=True,
        )
        reader.start()
        log(f"机器人已启动，PID={child.pid}")
        return child

    def _stop_child(self, reason: str) -> None:
        with self._child_lock:
            child = self._child
        if not child or child.poll() is not None:
            return

        log(f"{reason}，正在停止机器人 PID={child.pid}", "WARNING")
        try:
            child.terminate()
        except Exception as exc:
            log(f"结束机器人进程失败：{exc}", "WARNING")

        deadline = time.monotonic() + STOP_WAIT_SECONDS
        while child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.2)

        if child.poll() is None:
            log(f"机器人 PID={child.pid} 未按时退出，执行强制结束", "WARNING")
            try:
                child.kill()
            except Exception as exc:
                log(f"强制结束机器人失败：{exc}", "ERROR")

    def _consume_stop_file(self) -> bool:
        if not STOP_FILE.exists():
            return False
        self.request_stop("检测到 runtime\\supervisor.stop")
        _remove_file(STOP_FILE)
        return True

    def _wait_for_child(self, child: subprocess.Popen[str]) -> int:
        started = time.monotonic()
        next_health_check = started + HEALTH_INTERVAL_SECONDS
        startup_deadline = started + STARTUP_GRACE_SECONDS
        stale_reported = False

        while child.poll() is None:
            now = time.monotonic()
            if self._consume_stop_file():
                self._stop_child("看门狗停止")
                break

            if CLEAN_EXIT_FILE.exists():
                log("检测到正常退出标记，看门狗将一同退出")
                self.request_stop("机器人界面已正常退出")
                break

            if now >= next_health_check:
                next_health_check = now + HEALTH_INTERVAL_SECONDS
                heartbeat_age = _heartbeat_age()
                if now >= startup_deadline:
                    unhealthy_reason = ""
                    if heartbeat_age is None:
                        unhealthy_reason = "未收到机器人心跳"
                    elif heartbeat_age > HEARTBEAT_STALE_SECONDS:
                        unhealthy_reason = (
                            f"机器人心跳已停止 {heartbeat_age:.0f} 秒"
                        )
                    elif not _port_is_listening(BRIDGE_PORT):
                        unhealthy_reason = (
                            f"本地桥接端口 {BRIDGE_PORT} 未监听"
                        )

                    if unhealthy_reason:
                        log_error_message(
                            context="supervisor.health_check",
                            error_type="UnresponsiveBot",
                            error_message=unhealthy_reason,
                            extra={"pid": child.pid},
                        )
                        log(
                            f"检测到机器人无响应：{unhealthy_reason}",
                            "WARNING",
                        )
                        self._stop_child("机器人无响应")
                        break

                if (
                    heartbeat_age is not None
                    and heartbeat_age > HEARTBEAT_STALE_SECONDS
                    and not stale_reported
                ):
                    log(
                        f"机器人心跳延迟 {heartbeat_age:.0f} 秒，继续观察",
                        "WARNING",
                    )
                    stale_reported = True
                elif heartbeat_age is not None and (
                    heartbeat_age <= HEARTBEAT_STALE_SECONDS
                ):
                    stale_reported = False

            time.sleep(0.25)

        try:
            return int(child.wait(timeout=5))
        except subprocess.TimeoutExpired:
            return -1

    def _restart_delay(self) -> float:
        if self._consecutive_failures <= 0:
            return RESTART_DELAYS[0]
        index = min(
            self._consecutive_failures - 1,
            len(RESTART_DELAYS) - 1,
        )
        return RESTART_DELAYS[index]

    def _sleep_before_restart(self, delay: float) -> bool:
        log(f"{delay:.0f} 秒后自动重启机器人")
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if self._consume_stop_file():
                return False
            time.sleep(0.25)
        return True

    def run(self) -> int:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        _remove_file(STOP_FILE)
        if not self._acquire_instance_lock():
            log(
                f"端口 {SUPERVISOR_LOCK_PORT} 已被占用，"
                "已有看门狗在运行，本实例退出",
                "WARNING",
            )
            return 0
        mark_startup("supervisor")
        log("看门狗已启动，机器人崩溃或无响应时会自动重启")

        while not self._stopping:
            if self._consume_stop_file():
                break

            try:
                started = time.monotonic()
                child = self._start_child()
                exit_code = self._wait_for_child(child)
                runtime = time.monotonic() - started
            except Exception as exc:
                log_exception("supervisor.start_child", exc=exc)
                log(f"启动机器人失败：{exc}", "ERROR")
                exit_code = -1
                runtime = 0.0

            if self._stopping:
                break

            if exit_code == 0 and CLEAN_EXIT_FILE.exists():
                log("机器人正常退出，看门狗结束")
                break

            if runtime >= 180.0:
                self._consecutive_failures = 0
            else:
                self._consecutive_failures += 1

            log(
                f"机器人异常退出，退出码={exit_code}，"
                f"连续失败={self._consecutive_failures}",
                "ERROR",
            )
            log_error_message(
                context="supervisor.child_exit",
                error_type="BotProcessExit",
                error_message=(
                    f"机器人异常退出，退出码={exit_code}，"
                    f"连续失败={self._consecutive_failures}"
                ),
                extra={"runtime_seconds": round(runtime, 3)},
            )
            if not self._sleep_before_restart(self._restart_delay()):
                break

        self._stop_child("看门狗退出")
        _remove_file(HEARTBEAT_FILE)
        self._release_instance_lock()
        log("看门狗已停止")
        return 0


def main() -> int:
    supervisor = BotSupervisor()

    def handle_signal(signum, _frame) -> None:
        try:
            signal_name = signal.Signals(signum).name
        except Exception:
            signal_name = str(signum)
        supervisor.request_stop(f"收到信号 {signal_name}")

    for signal_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        candidate = getattr(signal, signal_name, None)
        if candidate is None:
            continue
        try:
            signal.signal(candidate, handle_signal)
        except (OSError, ValueError):
            pass

    return supervisor.run()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as exc:
        log_exception("supervisor.__main__", exc=exc, level="CRITICAL")
        raise
