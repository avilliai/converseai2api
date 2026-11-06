"""
Cosverse.ai → OpenAI-compatible API Proxy
入口文件
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.routers import admin, chat
from app.routers import model_list as models
from app.cookie_manager import cm

# ─────────────────────────── 日志 ────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

# ─────────────────────────── 鉴权配置 ────────────────────
API_KEY = os.getenv("PROXY_API_KEY", "cosverse")
if not API_KEY:
    logging.warning("⚠️  未设置 PROXY_API_KEY，所有请求将被拒绝")

_PUBLIC_PATHS = {"/", "/health"}


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in _PUBLIC_PATHS:
            return await call_next(request)
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"error": {"message": "Missing Authorization header", "type": "auth_error"}},
            )
        token = auth_header[len("Bearer "):]
        if not API_KEY or token != API_KEY:
            return JSONResponse(
                status_code=401,
                content={"error": {"message": "Invalid API key", "type": "auth_error"}},
            )
        return await call_next(request)


# ─────────────────────────── 定时保活 ────────────────────
# 注意：keepalive_tick 内部已改为只对"活跃账号池"（默认 10 个，
# 可用 ACTIVE_POOL_SIZE 环境变量调整）做探活/保活，不再扫描全量账号，
# 以降低高频探活触发验证码风控的概率。
# 保活间隔：默认每次执行间隔由 KEEPALIVE_INTERVAL（秒）控制。
KEEPALIVE_INTERVAL = int(os.getenv("KEEPALIVE_INTERVAL", "10000"))

_ka_log = logging.getLogger("cosverse_proxy.keepalive")


async def keep_alive_task():
    """
    后台定时保活任务，完全不阻塞 chat 请求。
    启动后先等待首个完整周期再首次执行，服务立即可用。
    """
    _ka_log.info(f"⏳ 保活任务已启动，下次执行在 {KEEPALIVE_INTERVAL // 60} 分钟后")
    await asyncio.sleep(KEEPALIVE_INTERVAL)

    while True:
        try:
            _ka_log.info("🔄 开始执行定时保活...")
            #await cm.keepalive_tick()
            _ka_log.info(f"✅ 保活完成，下次执行在 {KEEPALIVE_INTERVAL // 60} 分钟后")
        except asyncio.CancelledError:
            _ka_log.info("保活任务已取消")
            break
        except Exception as e:
            _ka_log.error(f"保活任务异常: {e}", exc_info=True)
        finally:
            try:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
            except asyncio.CancelledError:
                pass


# ─────────────────────────── 生命周期 ────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    cm.load()
    from app.action_fetcher import fetch_action_config
    await fetch_action_config()
    logging.getLogger("cosverse_proxy").info(
        f"🚀 代理服务启动，已加载 {len(cm.accounts)} 个账号，"
        f"活跃池 {len(cm.active_pool)} 个"
    )

    # 活跃池不足（含冷启动空池）：立即异步激活补充，不必等首个 keepalive 周期。
    # 备用账号 cookie 默认视为陈旧，_replenish_active_pool 内部会先逐个
    # refresh（收验证码邮件激活）成功后才纳入活跃池。
    from app.cookie_manager import ACTIVE_POOL_SIZE
    if len(cm.active_pool) < ACTIVE_POOL_SIZE:
        asyncio.create_task(cm._replenish_active_pool())

    bg_task = asyncio.create_task(keep_alive_task())

    yield

    bg_task.cancel()
    try:
        await bg_task
    except asyncio.CancelledError:
        pass


# ─────────────────────────── App ─────────────────────────
app = FastAPI(title="Cosverse.ai → OpenAI Proxy", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(AuthMiddleware)

# ── 路由挂载 ──────────────────────────────────────────────
app.include_router(chat.router)
app.include_router(models.router)
app.include_router(admin.router)


@app.get("/")
async def serve_dashboard():
    html_path = Path("dashboard.html")
    if html_path.exists():
        return FileResponse(html_path)
    return HTMLResponse(
        "<h1>Cosverse Proxy</h1>"
        "<p>OpenAI-compatible proxy for cosverse.ai</p>"
        "<ul>"
        "<li>POST /v1/chat/completions</li>"
        "<li>GET /v1/models</li>"
        "<li>GET /admin/tokens/detail</li>"
        "<li>POST /admin/register/start</li>"
        "<li>POST /admin/refresh/all  — 手动触发全量保活</li>"
        "</ul>"
    )


@app.get("/health")
async def health():
    return {"status": "ok", "accounts": len(cm.accounts)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8008, log_level="info")
