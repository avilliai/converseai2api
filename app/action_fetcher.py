"""
动态抓取 Next.js Server Action hashes 和 Deployment ID
"""

import re
import json
import logging
import urllib.parse
from curl_cffi.requests import AsyncSession
from app.config import PROXY

log = logging.getLogger("cosverse_proxy.action_fetcher")

COSVERSE_WEB = "https://chat.cosverse.ai"
IMPERSONATE  = "chrome110"
BASE_HEADERS = {
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
    "sec-ch-ua": '"Microsoft Edge";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0"
    ),
}


async def _fetch_text(session: AsyncSession, url: str, is_html: bool = False) -> str:
    headers = {
        **BASE_HEADERS,
        "accept": "text/html,application/xhtml+xml,*/*;q=0.9" if is_html else "*/*",
        "sec-fetch-dest": "document" if is_html else "script",
        "sec-fetch-mode": "navigate" if is_html else "no-cors",
        "sec-fetch-site": "none" if is_html else "same-origin",
    }
    resp = await session.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.text


def _extract_deployment_id(html: str) -> str:
    m = re.search(r'dpl_[A-Za-z0-9]+', html)
    return m.group(0) if m else ""


def _extract_chunk_urls(html: str) -> list[str]:
    """提取 JS chunk URL，兼容 Vercel 新增的 dpl 查询串。"""
    return list(dict.fromkeys(re.findall(
        r'(?:src|href)=["\'](/_next/static/[^"\']+\.js(?:\?[^"\']*)?)["\']',
        html,
    )))


def _extract_action_hashes(js: str) -> list[str]:
    """
    从 JS bundle 中提取 Server Action ID。
    Next.js 编译产物里注册形如：
      registerServerReference(fn,"40adc3232167f83c7900dd653f4b555c9debad554c",null)
    """
    found = set()
    for m in re.finditer(r'registerServerReference\s*\([^,]+,\s*"([0-9a-f]{40,44})"', js):
        found.add(m.group(1))
    if not found:
        for m in re.finditer(r'"([0-9a-f]{42})"', js):
            found.add(m.group(1))
    return list(found)


def _extract_named_actions(js: str) -> dict[str, str]:
    """从 Turbopack/webpack 产物中提取 action 名称到 hash 的映射。"""
    found: dict[str, str] = {}
    pattern = re.compile(
        r'createServerReference\)?\(\s*"([0-9a-f]{40,44})"'
        r'[^;]{0,400}?"([A-Za-z_$][A-Za-z0-9_$]*)"\s*\)'
    )
    for action_hash, action_name in pattern.findall(js):
        found[action_name] = action_hash
    return found


def _extract_router_state(html: str) -> str:
    m = re.search(r'next-router-state-tree["\s]*[:=]["\s]*([%A-Za-z0-9+/=]+)', html)
    if m:
        return m.group(1)
    return ""


class ActionConfig:
    """抓取到的动态配置，在进程生命周期内缓存"""
    email_action:  str = ""
    otp_action:    str = ""
    name_action:   str = ""
    title_action:  str = ""   # POST / 生成 chatTitle 的 Server Action
    usage_action:  str = ""   # POST /usage Server Action
    deployment_id: str = ""
    router_state:  str = ""   # 登录页专用
    _fetched:      bool = False

    def router_state_chat(self, conv_id: str) -> str:
        """
        chat 页的 next-router-state-tree，包含 conv_id。
        对应抓包里 /c/<conv_id> 页面发出请求时的 router state。
        """
        state = [
            "",
            {
                "children": [
                    ["params", f"c/{conv_id}", "oc", None],
                    {"children": ["__PAGE__", {}, None, None, 0]},
                    None, None, 0
                ]
            },
            None, None, 16
        ]
        return urllib.parse.quote(json.dumps(state, separators=(",", ":")))

    def router_state_usage(self) -> str:
        state = [
            "",
            {
                "children": [
                    ["params", "usage", "oc", None],
                    {"children": ["__PAGE__", {}, None, None, 0]},
                    None, None, 0
                ]
            },
            None, None, 16
        ]
        return urllib.parse.quote(json.dumps(state, separators=(",", ":")))


_cfg = ActionConfig()

# 已知的兜底值（网站未更新时仍可用）
_FALLBACK = {
    "email_action":  "400c4e30bdcce3a30a5d3f8b241cf5aa7a641def24",
    "otp_action":    "60fed0a1f5054c7d3703e93c55d809572b1250294e",
    "name_action":   "60a1d081ceb644ee662d33f415e23e034abdc32003",
    "title_action":  "40713336e55ac49e65d2159f352078f241da2e893a",  # 从抓包提取
    "usage_action":  "00f6fa84f07135389a18c99f948df07127c5ce6476",  # 从抓包提取 /usage ?? action
    "deployment_id": "dpl_AVKvTSHzb232JfJr7qS6m9dNE7bp",
    "router_state":  (
        "%5B%22%22%2C%7B%22children%22%3A%5B%22auth%22%2C%7B%22children%22%3A"
        "%5B%22signin%22%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2C"
        "null%2Cnull%2C0%5D%7D%2Cnull%2Cnull%2C0%5D%7D%2Cnull%2Cnull%2C0%5D"
        "%7D%2Cnull%2Cnull%2C16%5D"
    ),
}


async def fetch_action_config(force: bool = False) -> ActionConfig:
    """
    启动时（或 force=True）动态抓取最新的 action hashes。
    失败时使用 fallback 硬编码值并记录警告。
    """
    global _cfg
    if _cfg._fetched and not force:
        return _cfg

    log.info("🔍 正在动态抓取 Next.js action hashes ...")
    try:
        async with AsyncSession(impersonate=IMPERSONATE, timeout=30, proxy=PROXY or None) as session:
            # 1. 抓取登录页 HTML
            html = await _fetch_text(session, f"{COSVERSE_WEB}/auth/signin", is_html=True)

            # 2. 提取 deployment_id
            dep_id = _extract_deployment_id(html)
            log.info(f"  deployment_id: {dep_id or '未找到，使用 fallback'}")

            # 3. 提取 router_state（登录页专用）
            router = _extract_router_state(html)

            # 4. 找到所有 JS chunk URL
            chunk_urls = _extract_chunk_urls(html)
            log.info(f"  发现 {len(chunk_urls)} 个 JS chunk")

            # 5. 在 chunks 中搜索 action hashes
            all_hashes: set[str] = set()
            named_actions: dict[str, str] = {}
            for path in chunk_urls:
                url = f"{COSVERSE_WEB}{path}"
                try:
                    js = await _fetch_text(session, url)
                    hashes = _extract_action_hashes(js)
                    named_actions.update(_extract_named_actions(js))
                    if hashes:
                        all_hashes.update(hashes)
                        log.debug(f"  {path}: {hashes}")
                except Exception as e:
                    log.debug(f"  跳过 {path}: {e}")

            log.info(
                f"  共找到 {len(all_hashes)} 个 action hashes，"
                f"具名 actions: {sorted(named_actions)}"
            )

            def _pick(prefix: str, fallback: str) -> str:
                for h in all_hashes:
                    if h.startswith(prefix[:6]):
                        return h
                return fallback

            _cfg.email_action  = named_actions.get("sendOTP", _FALLBACK["email_action"])
            _cfg.otp_action    = named_actions.get("verifyOTP", _FALLBACK["otp_action"])
            _cfg.name_action   = named_actions.get("addUserName", _FALLBACK["name_action"])
            _cfg.title_action  = _pick(_FALLBACK["title_action"],  _FALLBACK["title_action"])
            _cfg.usage_action  = _pick(_FALLBACK["usage_action"],  _FALLBACK["usage_action"])
            _cfg.deployment_id = dep_id  or _FALLBACK["deployment_id"]
            _cfg.router_state  = router  or _FALLBACK["router_state"]
            _cfg._fetched      = True

            log.info(
                f"✅ action config 已就绪\n"
                f"  email : {_cfg.email_action}\n"
                f"  otp   : {_cfg.otp_action}\n"
                f"  name  : {_cfg.name_action}\n"
                f"  title : {_cfg.title_action}\n"
                f"  usage : {_cfg.usage_action}\n"
                f"  dep_id: {_cfg.deployment_id}"
            )

    except Exception as e:
        log.warning(f"⚠️  动态抓取失败 ({e})，使用 fallback 硬编码值")
        _cfg.email_action  = _FALLBACK["email_action"]
        _cfg.otp_action    = _FALLBACK["otp_action"]
        _cfg.name_action   = _FALLBACK["name_action"]
        _cfg.title_action  = _FALLBACK["title_action"]
        _cfg.usage_action  = _FALLBACK["usage_action"]
        _cfg.deployment_id = _FALLBACK["deployment_id"]
        _cfg.router_state  = _FALLBACK["router_state"]
        _cfg._fetched      = True

    return _cfg
