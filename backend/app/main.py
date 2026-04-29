import sys
import asyncio
import logging

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from pathlib import Path

from app.database import init_db


class AccessNoiseFilter(logging.Filter):
    """过滤高频轮询接口的访问日志，避免淹没有效诊断信息。"""

    NOISY_PATHS = (
        "/api/greeting/status",
        "/api/greeting/logs",
        "/api/automation/check-ready-state",
        "/api/config/stats",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            return not any(path in msg for path in self.NOISY_PATHS)
        except Exception:
            return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="Boss直聘自动化API",
    version="1.0.0",
    description="Boss直聘自动化工具后端API",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/health")
async def health_check():
    return {"status": "ok", "message": "Boss直聘自动化API运行正常"}

from app.routes import automation, candidates, templates, config, logs, greeting, accounts, notification, automation_templates, position_keywords

app.include_router(automation.router)
app.include_router(candidates.router)
app.include_router(templates.router)
app.include_router(automation_templates.router)
app.include_router(config.router)
app.include_router(logs.router)
app.include_router(greeting.router)
app.include_router(accounts.router)
app.include_router(notification.router)
app.include_router(position_keywords.router)

frontend_dist = Path(__file__).parent.parent.parent / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    import os

    if sys.platform == 'win32':
        import multiprocessing
        multiprocessing.freeze_support()

    port = int(os.getenv("API_PORT", os.getenv("PORT", 27421)))
    host = os.getenv("API_HOST", "0.0.0.0")
    is_frozen = getattr(sys, 'frozen', False)

    # 降噪 Uvicorn 访问日志，只保留关键请求
    logging.getLogger("uvicorn.access").addFilter(AccessNoiseFilter())

    if is_frozen or sys.platform == 'win32':
        uvicorn.run(app, host=host, port=port, reload=False)
    else:
        uvicorn.run("app.main:app", host=host, port=port, reload=True)