"""微信聊天机器人 - 仅 Web 控制台入口。"""

import sys
import time

import uvicorn
from loguru import logger

from main import (
    ChatBot,
    _report_duplicate_start,
    acquire_single_instance,
)
from src.error_logger import log_exception
from src.web.app import app as web_app


def main() -> None:
    if not acquire_single_instance():
        message = (
            "已经有一个微信机器人在运行了，请勿重复启动。"
            "如需重启，请先运行 restart-clean.bat 清理旧实例。"
        )
        _report_duplicate_start(message)
        sys.exit(1)

    bot = ChatBot()
    if not bot.initialize():
        print("初始化失败，请检查配置文件")
        sys.exit(1)

    web_cfg = bot.config.get("web", {})
    host = web_cfg.get("host", "127.0.0.1")
    port = web_cfg.get("port", 8080)
    logger.info(f"Web 控制台: http://{host}:{port}")
    for attempt in range(1, 4):
        try:
            uvicorn.run(web_app, host=host, port=port, log_level="warning")
            return
        except KeyboardInterrupt:
            return
        except BaseException as exc:
            log_exception("web_main.run", exc=exc)
            logger.error(f"Web 控制台启动失败（第 {attempt}/3 次）: {exc}")
            time.sleep(2 * attempt)


if __name__ == "__main__":
    main()
