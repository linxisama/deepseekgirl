"""永久错误日志：追加写入、每次启动分段、轮转但永不自动删除。"""

from __future__ import annotations

import logging
import os
import platform
import sys
import threading
import traceback
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from types import TracebackType
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"
ERROR_LOG = LOG_DIR / "errors.log"

_LOGGER_LOCK = threading.Lock()
_LOGGER: logging.Logger | None = None
_HOOKS_INSTALLED = False
_ORIGINAL_EXCEPTHOOK = sys.excepthook
_ORIGINAL_THREAD_EXCEPTHOOK = getattr(threading, "excepthook", None)
_SEPARATOR = "=" * 100


def _build_logger() -> logging.Logger:
    global _LOGGER
    if _LOGGER is not None:
        return _LOGGER

    with _LOGGER_LOCK:
        if _LOGGER is not None:
            return _LOGGER

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger("wechat_robot.persistent_errors")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.handlers.clear()

        handler = TimedRotatingFileHandler(
            ERROR_LOG,
            when="midnight",
            interval=1,
            backupCount=0,
            encoding="utf-8",
            delay=True,
            utc=False,
        )
        handler.suffix = "%Y-%m-%d"
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-8s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logger.addHandler(handler)
        logging.raiseExceptions = False
        _LOGGER = logger
        return logger


def error_log_path() -> Path:
    """返回当前永久错误日志路径。"""
    return ERROR_LOG


def mark_startup(component: str = "robot") -> None:
    """写入启动分隔标记，方便按段排查。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    message = (
        f"{_SEPARATOR}\n"
        f"=== START {now} | component={component} | pid={os.getpid()} | "
        f"python={platform.python_version()} | cwd={Path.cwd()} ===\n"
        f"{_SEPARATOR}"
    )
    _build_logger().info(message)


def _format_exception(
    exc_type: type[BaseException] | None,
    exc_value: BaseException | None,
    exc_tb: TracebackType | None,
) -> tuple[str, str, str]:
    if exc_type is None and exc_value is None and exc_tb is None:
        return "无异常对象", "没有可用异常信息", "调用处的错误被记录，但没有异常堆栈。"

    error_type = (
        exc_type.__name__
        if exc_type is not None
        else type(exc_value).__name__ if exc_value is not None else "UnknownError"
    )
    error_message = str(exc_value) if exc_value is not None else "无错误消息"
    stack = "".join(
        traceback.format_exception(exc_type, exc_value, exc_tb)
    ).rstrip()
    if not stack:
        stack = "调用处的错误被记录，但没有可用堆栈。"
    return error_type, error_message, stack


def log_error_message(
    context: str,
    error_type: str,
    error_message: str,
    stack: str = "",
    level: str = "ERROR",
    extra: dict[str, Any] | None = None,
) -> None:
    """记录一条可检索的完整错误段。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    thread_name = threading.current_thread().name
    lines = [
        "",
        _SEPARATOR,
        f"=== ERROR {now} ===",
        f"时间: {now}",
        f"进程: pid={os.getpid()}",
        f"线程: {thread_name}",
        f"场景: {context}",
        f"错误类型: {error_type}",
        f"错误信息: {error_message}",
    ]
    if extra:
        lines.append(f"附加信息: {extra}")
    lines.extend(
        [
            "完整堆栈:",
            stack or "没有可用堆栈。",
            _SEPARATOR,
        ]
    )
    _build_logger().log(
        getattr(logging, level.upper(), logging.ERROR),
        "\n".join(lines),
    )


def log_exception(
    context: str,
    exc_type: type[BaseException] | None = None,
    exc_value: BaseException | None = None,
    exc_tb: TracebackType | None = None,
    level: str = "ERROR",
    extra: dict[str, Any] | None = None,
    exc: BaseException | None = None,
) -> None:
    """记录异常类型、消息和完整 traceback。"""
    if exc is not None and exc_value is None:
        exc_type = type(exc)
        exc_value = exc
        exc_tb = exc.__traceback__
    if exc_type is None and exc_value is None and exc_tb is None:
        exc_type, exc_value, exc_tb = sys.exc_info()

    error_type, error_message, stack = _format_exception(
        exc_type,
        exc_value,
        exc_tb,
    )
    log_error_message(
        context=context,
        error_type=error_type,
        error_message=error_message,
        stack=stack,
        level=level,
        extra=extra,
    )


def install_exception_hooks() -> None:
    """安装主线程和子线程未捕获异常钩子，重复调用不叠加。"""
    global _HOOKS_INSTALLED
    if _HOOKS_INSTALLED:
        return

    def exception_hook(exc_type, exc_value, exc_tb) -> None:
        log_exception(
            "主线程未捕获异常",
            exc_type,
            exc_value,
            exc_tb,
            level="CRITICAL",
        )
        _ORIGINAL_EXCEPTHOOK(exc_type, exc_value, exc_tb)

    def thread_exception_hook(args) -> None:
        log_exception(
            f"线程 {args.thread.name} 未捕获异常",
            args.exc_type,
            args.exc_value,
            args.exc_traceback,
            level="CRITICAL",
            extra={"thread_name": args.thread.name},
        )
        if _ORIGINAL_THREAD_EXCEPTHOOK is not None:
            _ORIGINAL_THREAD_EXCEPTHOOK(args)

    sys.excepthook = exception_hook
    if hasattr(threading, "excepthook"):
        threading.excepthook = thread_exception_hook
    _HOOKS_INSTALLED = True


def log_loguru_error(message: Any) -> None:
    """把 loguru 的 ERROR/CRITICAL 写入永久错误日志。"""
    record = message.record
    context = (
        f"{record['name']}:{record['function']}:{record['line']} | "
        f"{record['message']}"
    )
    level = record["level"].name
    exception = record.get("exception")
    if exception:
        log_exception(
            context,
            exception.type,
            exception.value,
            exception.traceback,
            level=level,
        )
        return

    exc_type, exc_value, exc_tb = sys.exc_info()
    if exc_type is not None:
        log_exception(
            context,
            exc_type,
            exc_value,
            exc_tb,
            level=level,
        )
        return

    log_error_message(
        context=context,
        error_type="LoggedError",
        error_message=record["message"],
        stack="调用点没有活动异常，通常这是业务层主动记录的错误。",
        level=level,
        extra={
            "module": record["name"],
            "function": record["function"],
            "line": record["line"],
        },
    )
