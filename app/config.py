"""
全局常量与配置
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── 文件路径 ──────────────────────────────────────────────
TOKENS_FILE = Path(os.getenv("TOKENS_FILE", "./cosverse_tokens.json"))
OUTLOOK_FILE = Path(os.getenv("OUTLOOK_FILE", "./outlook_live_20261018_222014.txt"))
OUTLOOK_POINTER_FILE = Path(os.getenv("OUTLOOK_POINTER_FILE", "./outlook_pointer.json"))

# ── 上游地址 ──────────────────────────────────────────────
COSVERSE_WEB = "https://chat.cosverse.ai"
COSVERSE_API = "https://chat.cosverse.ai/api"

# ── 默认 HTTP 请求头 ────────────────────────────────────────
COMMON_HEADERS: dict[str, str] = {
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
    "sec-ch-ua": '"Microsoft Edge";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0"
    ),
}

# ── 代理配置 ──────────────────────────────────────────────
PROXY = os.getenv("PROXY") or os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY") or ""
HTTP_PROXY = PROXY
HTTPS_PROXY = PROXY

if PROXY:
    os.environ["http_proxy"] = PROXY
    os.environ["https_proxy"] = PROXY
    os.environ["HTTP_PROXY"] = PROXY
    os.environ["HTTPS_PROXY"] = PROXY
    os.environ["ALL_PROXY"] = PROXY
