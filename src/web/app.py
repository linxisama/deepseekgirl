"""FastAPI 应用初始化"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

from .routes import router
from .websocket import ws_router

app = FastAPI(title="微信聊天机器人", version="1.0.0")

# 注册路由
app.include_router(router, prefix="/api")
app.include_router(ws_router)

# 静态文件
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    """返回仪表盘页面"""
    index_file = static_dir / "index.html"
    if index_file.exists():
        return index_file.read_text(encoding="utf-8")
    return "<h1>微信聊天机器人 - 仪表盘</h1><p>静态文件未找到</p>"
