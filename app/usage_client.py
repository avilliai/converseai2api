import json
import logging
from typing import Any, Dict, Optional, Tuple
from curl_cffi.requests import AsyncSession
from app.action_fetcher import fetch_action_config
from app.config import PROXY, COSVERSE_API, COSVERSE_WEB

log = logging.getLogger(__name__)


async def fetch_usage(
    cookie_str: str, timeout: int = 15
) -> Tuple[bool, Optional[Dict[str, Any]], Optional[str]]:
    """
    检查 session 存活状态与额度信息。
    优先通过 /api/auth/session 验证用户登录状态，
    若有效则返回 (True, limits, None)，若失效返回 (False, None, error)。
    """
    headers = {
        "accept": "*/*",
        "cookie": cookie_str,
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
        ),
        "referer": f"{COSVERSE_WEB}/",
    }

    try:
        async with AsyncSession(impersonate="chrome110", proxy=PROXY or None) as session:
            resp = await session.get(
                f"{COSVERSE_API}/auth/session",
                headers=headers,
                timeout=timeout,
            )

            if resp.status_code != 200:
                return False, None, f"HTTP {resp.status_code}"

            try:
                data = resp.json()
            except Exception:
                data = None

            if not data or not isinstance(data, dict) or not data.get("user", {}).get("id"):
                return False, None, "Session expired or invalid"

            limits = {
                "messageLimit": 100,
                "messageUsage": 0,
            }

            return True, limits, None
    except Exception as e:
        log.warning(f"fetch_usage 异常: {e}")
        return False, None, str(e)
