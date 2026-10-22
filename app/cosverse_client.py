"""
Cosverse.ai 注册与对话核心逻辑
"""
import asyncio
import json
import logging
import mimetypes
import os
import random
import re
import string
import threading
import time
from collections import deque
from email import message_from_string
from typing import Any, AsyncIterator, Optional

from curl_cffi import CurlMime
from curl_cffi.requests import AsyncSession

from app.config import COSVERSE_WEB, COSVERSE_API, COMMON_HEADERS, PROXY
from app.action_fetcher import fetch_action_config
from app.models import AccountState
from app.email_service import EmailService
from app.outlook_email_service import (
    OutlookAccount,
    OutlookAccountPool,
    get_outlook_pool,
    get_folder_highest_uids,
    wait_for_outlook_code,
)

log = logging.getLogger("cosverse_proxy.client")
IMPERSONATE = "chrome110"


class EmptyResponseError(RuntimeError):
    """流式响应内容为空（content_tokens=0），视作 cookie 过期或账号异常。"""
    pass

class ServerError(RuntimeError):
    """Cosverse 服务端应用层错误（如 invalid_grant），无需重试，直接透传给调用方。"""
    pass


class OtpSendBlockedError(RuntimeError):
    """OTP 邮件发送被本地限流器或熔断器阻止。"""


class _OtpSendGate:
    """跨线程/跨事件循环的 OTP 全局限流与熔断器。"""

    def __init__(self) -> None:
        self.min_interval = max(0.0, float(os.getenv("OTP_MIN_INTERVAL_SECONDS", "12")))
        self.window_seconds = max(1.0, float(os.getenv("OTP_WINDOW_SECONDS", "300")))
        self.max_per_window = max(1, int(os.getenv("OTP_MAX_PER_WINDOW", "4")))
        self.cooldown_seconds = max(1.0, float(os.getenv("OTP_COOLDOWN_SECONDS", "1800")))
        self.per_email_cooldown = max(
            0.0, float(os.getenv("OTP_PER_EMAIL_COOLDOWN_SECONDS", "300"))
        )
        self._lock = threading.Lock()
        self._sent_at: deque[float] = deque()
        self._last_by_email: dict[str, float] = {}
        self._next_slot_at = 0.0
        self._blocked_until = 0.0
        self._blocked_reason = ""

    async def acquire(self, email: str) -> None:
        normalized = email.strip().lower()
        while True:
            with self._lock:
                now = time.monotonic()
                if now < self._blocked_until:
                    remaining = int(self._blocked_until - now) + 1
                    raise OtpSendBlockedError(
                        f"OTP 邮箱通道已熔断，{remaining}s 后重试"
                        + (f"（{self._blocked_reason}）" if self._blocked_reason else "")
                    )

                while self._sent_at and now - self._sent_at[0] >= self.window_seconds:
                    self._sent_at.popleft()

                waits = [max(0.0, self._next_slot_at - now)]
                last_for_email = self._last_by_email.get(normalized, 0.0)
                waits.append(max(0.0, last_for_email + self.per_email_cooldown - now))
                if len(self._sent_at) >= self.max_per_window:
                    waits.append(max(0.0, self._sent_at[0] + self.window_seconds - now))
                wait_for = max(waits)

                if wait_for <= 0:
                    self._sent_at.append(now)
                    self._last_by_email[normalized] = now
                    self._next_slot_at = now + self.min_interval
                    return

            log.warning(f"[{email}] OTP 发送限流，等待 s")
            await asyncio.sleep(3)

    def trip(self, reason: str, seconds: Optional[float] = None) -> None:
        duration = self.cooldown_seconds if seconds is None else max(1.0, seconds)
        with self._lock:
            self._blocked_until = max(self._blocked_until, time.monotonic() + duration)
            self._blocked_reason = reason[:160]
        log.error(f"OTP 邮箱通道触发熔断 {int(duration)}s：{reason}")


_OTP_SEND_GATE = _OtpSendGate()

R2_UPLOAD_URL = f"{COSVERSE_WEB}/api/r2-upload"


# ── 工具函数 ──────────────────────────────────────────────

def _random_name(length: int = 8) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))

def _random_conv_id(length: int = 16) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))

def _merge_cookies(existing: dict, incoming: dict) -> dict:
    return {**existing, **incoming}

def _cookies_dict_to_str(cookies: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())

def _nextjs_headers(action: str, deployment_id: str, router_state: str) -> dict:
    return {
        **COMMON_HEADERS,
        "accept":                 "text/x-component",
        "content-type":           "text/plain;charset=UTF-8",
        "next-action":            action,
        "next-router-state-tree": router_state,
        "priority":               "u=1, i",
        "sec-fetch-dest":         "empty",
        "sec-fetch-mode":         "cors",
        "sec-fetch-site":         "same-origin",
        "x-deployment-id":        deployment_id,
        "referer":                f"{COSVERSE_WEB}/auth/signin",
    }

def _parse_action_response(text: str) -> Optional[Any]:
    try:
        for line in text.strip().split("\n"):
            if line.startswith("1:"):
                return json.loads(line[2:])
    except Exception:
        pass
    return None

def _extract_otp_from_email(raw_email: str) -> Optional[str]:
    if not raw_email:
        return None
    msg = message_from_string(raw_email)
    content = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if payload:
                    content = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            content = payload.decode("utf-8", errors="ignore")
    content = re.sub(r"<[^>]+>", " ", content)
    content = re.sub(r"\s+", " ", content).strip()
    for pattern in [r"(?:code|验证码|OTP).*?(\d{6})", r"(?<!\d)(\d{6})(?!\d)"]:
        m = re.search(pattern, content, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


# ── R2 文件上传 ───────────────────────────────────────────

import base64


async def upload_file_to_cosverse(
        data: bytes,
        filename: str,
        acc: AccountState,
        chat_id: str = "",
        content_type: str = "",
) -> Optional[dict]:
    """
    将文件上传到 Cosverse R2 存储，返回 {"key": ..., "publicUrl": ..., "dataUri": ...} 或 None。
    chat_id 必须与后续 stream_cosverse_response 使用的 conv_id 一致。
    """
    if not content_type:
        content_type, _ = mimetypes.guess_type(filename)
        content_type = content_type or "application/octet-stream"

    if not chat_id:
        chat_id = _random_conv_id()

    mp = CurlMime()
    try:
        mp.addpart(
            name="file",
            filename=filename,
            data=data,
            content_type=content_type,
        )
        mp.addpart(
            name="chatId",
            data=chat_id.encode("utf-8") if isinstance(chat_id, str) else chat_id,
            content_type="text/plain",
        )

        headers = {
            **COMMON_HEADERS,
            "accept": "*/*",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "referer": f"{COSVERSE_WEB}/",
        }
        headers.pop("content-type", None)
        headers.pop("Content-Type", None)

        async with AsyncSession(impersonate=IMPERSONATE, timeout=60, proxy=PROXY or None) as session:
            resp = await session.post(
                R2_UPLOAD_URL,
                headers=headers,
                multipart=mp,
                cookies=acc.cookies_dict(),
                timeout=60,
            )
            if resp.status_code in (200, 201):
                result = resp.json()
                b64_str = base64.b64encode(data).decode('utf-8')
                result["dataUri"] = f"data:{content_type};base64,{b64_str}"
                log.info(f"[{acc.email}] 文件上传成功: {result.get('publicUrl', '')}")
                return result
            else:
                log.warning(f"[{acc.email}] 文件上传失败: HTTP {resp.status_code} - {resp.text[:200]}")
                return None
    except Exception as e:
        log.error(f"[{acc.email}] 文件上传异常: {e}")
        return None
    finally:
        mp.close()


async def upload_url_to_cosverse(
    url: str,
    acc: AccountState,
    chat_id: str = "",
) -> Optional[dict]:
    """
    从 URL 下载图片/文件后上传到 Cosverse R2。
    支持 data URI（base64）和 http/https URL。
    """
    import re as _re

    try:
        data_uri_match = _re.match(
            r"data:(?P<mime>[^;,]+)(?:;base64)?,(?P<data>.+)", url, _re.DOTALL
        )
        if data_uri_match:
            mime  = data_uri_match.group("mime").strip()
            b64   = data_uri_match.group("data").strip()
            raw   = base64.b64decode(b64)
            ext   = mimetypes.guess_extension(mime) or ""
            fname = f"upload{ext or '.bin'}"
            return await upload_file_to_cosverse(raw, fname, acc, chat_id, mime)

        async with AsyncSession(impersonate=IMPERSONATE, timeout=30, proxy=PROXY or None) as session:
            resp = await session.get(url, timeout=30)
            if resp.status_code != 200:
                log.warning(f"下载文件失败: {url} -> HTTP {resp.status_code}")
                return None
            raw          = resp.content
            content_type = resp.headers.get("content-type", "").split(";")[0].strip()
            ext          = mimetypes.guess_extension(content_type) or ""
            path_part    = url.split("?")[0].rstrip("/").split("/")[-1]
            fname        = path_part if "." in path_part else f"download{ext or '.bin'}"
            return await upload_file_to_cosverse(raw, fname, acc, chat_id, content_type)

    except Exception as e:
        log.error(f"upload_url_to_cosverse 失败: {url[:80]} → {e}")
        return None


# ── 登录公共 Mixin ────────────────────────────────────────

class _LoginMixin:
    """
    封装 email_action / otp_action / nextauth 三步登录的公共实现，
    供 CosverseRegister 和 CosverseRefresher 共同继承，避免重复代码。
    """

    def _absorb(self, resp_cookies) -> None:
        self._cookies = _merge_cookies(self._cookies, dict(resp_cookies))

    async def _post_action(self, session: AsyncSession, action: str, body: str) -> tuple[int, str]:
        async def _post(current_action: str):
            return await session.post(
                f"{COSVERSE_WEB}/auth/signin",
                headers=_nextjs_headers(
                    current_action,
                    self._cfg.deployment_id,
                    self._cfg.router_state,
                ),
                data=body,
                cookies=self._cookies or None,
                timeout=20,
            )

        resp = await _post(action)
        self._absorb(resp.cookies)

        stale_hint = resp.text.lower()
        stale_action = resp.status_code in (404, 410) or any(
            hint in stale_hint
            for hint in (
                "failed to find server action",
                "server action was not found",
                "unknown server action",
            )
        )
        if stale_action:
            action_field = next(
                (
                    field
                    for field in ("email_action", "otp_action", "name_action")
                    if action == getattr(self._cfg, field, None)
                ),
                "",
            )
            self._cfg = await fetch_action_config(force=True)
            refreshed_action = (
                getattr(self._cfg, action_field, action) if action_field else action
            )
            log.warning("检测到 Server Action 已失效，刷新配置后重试一次")
            resp = await _post(refreshed_action)
            self._absorb(resp.cookies)
        return resp.status_code, resp.text

    async def _step_send_email(
        self, session: AsyncSession, email: str
    ) -> Optional[dict[str, Any]]:
        #await _OTP_SEND_GATE.acquire(email)
        status, text = await self._post_action(
            session, self._cfg.email_action, json.dumps([email])
        )
        log.info(f"[{email}] send_email status={status} resp={text[:200]}")
        data = _parse_action_response(text)
        error = ""
        if isinstance(data, dict):
            error = str(data.get("error") or "")
        response_hint = f"{error} {text[:500]}".lower()
        protection_keywords = (
            "too many", "rate limit", "rate-limit", "temporarily disabled",
            "email disabled", "email service", "try again later", "429",
        )
        if status in (403, 429) or any(k in response_hint for k in protection_keywords):
            _OTP_SEND_GATE.trip(error or f"HTTP {status}")
        if status not in (200, 201) or data is None:
            return None
        if isinstance(data, dict):
            if data.get("error") or data.get("success") is False:
                return None
            return data
        return {"success": bool(data)}

    async def _step_verify_otp(self, session: AsyncSession, email: str, otp: str) -> bool:
        status, text = await self._post_action(
            session, self._cfg.otp_action, json.dumps([email, otp])
        )
        log.info(f"[{email}] verify_otp status={status} resp={text[:300]}")
        data = _parse_action_response(text)
        if status not in (200, 201) or data is None:
            return False
        if isinstance(data, dict):
            return not data.get("error") and data.get("success") is not False
        return bool(data)

    async def _step_nextauth_login(self, session: AsyncSession, email: str, otp: str) -> str:
        """
        走 csrf → callback/credentials → session 流程。
        成功返回 user_id 字符串，失败返回空字符串。
        """
        from urllib.parse import quote

        _h_common = {
            **COMMON_HEADERS,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "referer": f"{COSVERSE_WEB}/auth/signin",
        }

        # 4a. 获取 CSRF token
        csrf_resp = await session.get(
            f"{COSVERSE_API}/auth/csrf",
            headers={**_h_common, "accept": "*/*", "content-type": "application/json"},
            cookies=self._cookies or None,
            timeout=15,
        )
        self._absorb(csrf_resp.cookies)
        csrf_token = csrf_resp.json().get("csrfToken", "")
        log.info(f"[{email}] csrfToken={'✓' if csrf_token else '✗'}")

        # 4b. callback/credentials
        cb_body = (
            f"email={quote(email)}&otp={otp}"
            f"&csrfToken={csrf_token}"
            f"&callbackUrl={quote(COSVERSE_WEB + '/auth/signin')}"
        )
        cb_resp = await session.post(
            f"{COSVERSE_API}/auth/callback/credentials",
            headers={**_h_common, "accept": "*/*",
                     "content-type": "application/x-www-form-urlencoded",
                     "x-auth-return-redirect": "1"},
            data=cb_body,
            cookies=self._cookies or None,
            timeout=15,
        )
        self._absorb(cb_resp.cookies)
        log.info(f"[{email}] callback status={cb_resp.status_code}")

        # 4c. 验证 session
        sess_resp = await session.get(
            f"{COSVERSE_API}/auth/session",
            headers={**_h_common, "accept": "*/*", "content-type": "application/json"},
            cookies=self._cookies or None,
            timeout=15,
        )
        self._absorb(sess_resp.cookies)
        user_id = sess_resp.json().get("user", {}).get("id", "")
        log.info(f"[{email}] session user_id={user_id}")
        return user_id

    async def _wait_for_otp(self, email_service: EmailService, jwt: str,
                             email: str, timeout: int = 120) -> str:
        import asyncio
        for i in range(timeout // 6):
            raw = email_service.fetch_first_email(jwt)
            if raw:
                otp = _extract_otp_from_email(raw)
                if otp:
                    log.info(f"[{email}] 第 {i+1} 次检查到 OTP: {otp}")
                    return otp
            log.info(f"[{email}] 第 {i+1} 次检查邮件...")
            await asyncio.sleep(6)
        return ""


# ── 注册客户端 ────────────────────────────────────────────

class CosverseRegister(_LoginMixin):
    """
    注册客户端：
    优先使用 Outlook 邮箱账号池（outlook_live_20261018_222014.txt）。
    流程：
    Step 1  发送邮箱（触发 OTP）
    Step 2  获取 OTP（Outlook IMAP 或 Worker 邮件服务）
    Step 3  如果为新用户，执行 verify_otp 和 addUserName
    Step 4  nextauth 登录拿 session cookie
    """

    def __init__(self, outlook_acc: Optional[OutlookAccount] = None):
        self.outlook_acc = outlook_acc
        self.email: str = outlook_acc.address if outlook_acc else ""
        self.name: str = _random_name()
        self.otp: str = ""
        self._cookies: dict = {}
        self.user_id: str = ""
        self._jwt: str = ""
        self._cfg = None

    async def _step_set_name(self, session: AsyncSession) -> bool:
        status, text = await self._post_action(
            session, self._cfg.name_action, json.dumps([self.email, self.name])
        )
        log.info(f"[{self.email}] Step3 name={self.name} status={status} resp={text[:200]}")
        data = _parse_action_response(text)
        return bool(data and data.get("success"))

    async def run(self) -> Optional[dict]:
        try:
            self._cfg = await fetch_action_config()

            # 1. 准备账号
            is_outlook = False
            baseline_folders = {'"Junk"': 0, "INBOX": 0}
            if not self.outlook_acc:
                try:
                    pool = get_outlook_pool()
                    self.outlook_acc = await pool.get_next_account()
                except Exception as e:
                    log.warning(f"从 Outlook 账号池取号失败或文件不存在: {e}")

            if self.outlook_acc:
                is_outlook = True
                self.email = self.outlook_acc.address
                self.name = "User" + self.email.split("@")[0][-4:]
                # 获取发码前的基线 UID
                baseline_folders = await asyncio.to_thread(
                    get_folder_highest_uids,
                    self.outlook_acc.address,
                    self.outlook_acc.client_id,
                    self.outlook_acc.refresh_token,
                )
            else:
                # 回退到 worker 邮箱
                email_service = EmailService()
                self._jwt, self.email = email_service.create_email()
                if not self.email:
                    raise RuntimeError("邮箱创建失败")

            log.info(f"[{self.email}] 开始注册/登录流程 (is_outlook={is_outlook})...")

            async with AsyncSession(impersonate=IMPERSONATE, timeout=60, proxy=PROXY or None) as session:
                otp_request = await self._step_send_email(session, self.email)
                if not otp_request:
                    raise RuntimeError("Step1 发送邮箱失败")
                is_new_user = bool(otp_request.get("newUser"))

                # Step 2: 获取 OTP
                if is_outlook:
                    self.otp = await wait_for_outlook_code(
                        self.outlook_acc.address,
                        self.outlook_acc.client_id,
                        self.outlook_acc.refresh_token,
                        baseline_folders=baseline_folders,
                        timeout=90,
                    )
                else:
                    self.otp = await self._wait_for_otp(
                        email_service, self._jwt, self.email
                    )

                if not self.otp:
                    raise RuntimeError("获取 OTP 超时")

                # Step 3: NextAuth 凭据登录拿到 session cookie（注册与登录共用统一接口）
                self.user_id = await self._step_nextauth_login(
                    session, self.email, self.otp
                )
                if not self.user_id:
                    raise RuntimeError("Step3 next-auth 登录/注册失败")

            cookie_str = _cookies_dict_to_str(self._cookies)
            limit, usage = 100, 0
            healthy = True
            try:
                from app.usage_client import fetch_usage
                alive, limits, _ = await fetch_usage(cookie_str)
                if alive and limits:
                    limit = int(limits.get("messageLimit", 100) or 100)
                    usage = int(limits.get("messageUsage", 0) or 0)
                healthy = alive
            except Exception as e:
                log.warning(f"[{self.email}] fetch_usage 失败: {e}")

            account = {
                "email": self.email,
                "name": self.name,
                "cookie_str": cookie_str,
                "user_id": self.user_id,
                "healthy": healthy,
                "disabled": False,
                "usage_count": usage,
                "message_limit": limit,
                "message_usage": usage,
                "quota_exhausted": False,
            }

            if is_outlook:
                account["password"] = self.outlook_acc.password
                account["client_id"] = self.outlook_acc.client_id
                account["refresh_token"] = self.outlook_acc.refresh_token
            if self._jwt:
                account["email_jwt"] = self._jwt

            log.info(f"[{self.email}] ✅ 注册/登录成功 user_id={self.user_id}")
            if is_outlook and self.outlook_acc:
                try:
                    pool = get_outlook_pool()
                    await pool.mark_success(self.outlook_acc.address)
                except Exception as ex:
                    log.warning(f"登记 Outlook 账号成功状态失败: {ex}")
            return account

        except Exception as e:
            log.error(f"CosverseRegister.run() 失败: {e}")
            if is_outlook and self.outlook_acc:
                try:
                    pool = get_outlook_pool()
                    err_str = str(e).lower()
                    is_perm = any(k in err_str for k in ("invalid_grant", "bad credentials", "account disabled", "user not found", "user disabled"))
                    await pool.mark_failed(self.outlook_acc.address, str(e), is_permanent=is_perm)
                except Exception as ex:
                    log.warning(f"登记 Outlook 账号失败状态异常: {ex}")
            import traceback
            traceback.print_exc()
            return None


# ── 保活/重新登录客户端 ────────────────────────────────────

class CosverseRefresher(_LoginMixin):
    """
    保活/重新登录客户端：
    如果账号带有 Outlook 凭据（client_id / refresh_token），直接通过 Outlook IMAP 重新登录并刷新 session cookie。
    如果是旧版 Worker 邮箱账号，则通过 email_jwt 刷新。
    成功后直接将新 cookie 写回 AccountState 并标记为健康。
    """

    def __init__(self, acc: AccountState):
        self.acc = acc
        self.email = acc.email
        self.otp: str = ""
        self._cookies: dict = {}
        self._cfg = None
        self._email_jwt: str = getattr(acc, "email_jwt", "")

    async def _refresh_outlook_account(self) -> bool:
        """使用 Outlook 凭据重新登录"""
        try:
            client_id = getattr(self.acc, "client_id", "")
            refresh_token = getattr(self.acc, "refresh_token", "")
            if not client_id or not refresh_token:
                log.warning(f"[{self.email}] 缺少 client_id 或 refresh_token，无法进行 Outlook 保活")
                return False

            baseline_folders = await asyncio.to_thread(
                get_folder_highest_uids,
                self.email,
                client_id,
                refresh_token,
            )

            async with AsyncSession(impersonate=IMPERSONATE, timeout=60, proxy=PROXY or None) as session:
                otp_request = await self._step_send_email(session, self.email)
                if not otp_request:
                    return False

                is_new_user = bool(otp_request.get("newUser"))
                self.otp = await wait_for_outlook_code(
                    self.email,
                    client_id,
                    refresh_token,
                    baseline_folders=baseline_folders,
                    timeout=90,
                )
                if not self.otp:
                    return False

                # 注册与登录统一走 NextAuth credentials
                user_id = await self._step_nextauth_login(session, self.email, self.otp)
                if not user_id:
                    return False

                self.acc.cookie_str = _cookies_dict_to_str(self._cookies)
                self.acc.user_id = user_id
                self.acc.healthy = True
                self.acc.disabled = False
                self.acc.fail_count = 0
                return True
        except Exception as e:
            log.warning(f"[{self.email}] Outlook 保活重新登录异常: {e}")
            return False

    async def _recover_email_jwt(self) -> str:
        try:
            from app.email_service import EmailService as ES
            es = ES()
            name = self.email.split("@")[0]
            jwt, _ = await asyncio.wait_for(
                asyncio.to_thread(es.recover_email_jwt, name),
                timeout=20,
            )
            if jwt:
                self.acc.email_jwt = jwt
                self._email_jwt = jwt
            return jwt or ""
        except Exception as e:
            return ""

    async def run(self) -> bool:
        try:
            self._cfg = await fetch_action_config()

            # 优先尝试 Outlook 重新登录保活
            if getattr(self.acc, "client_id", "") and getattr(self.acc, "refresh_token", ""):
                log.info(f"[{self.email}] 触发 Outlook OAuth IMAP 重新登录保活...")
                return await self._refresh_outlook_account()

            # 否则尝试 Worker 邮箱保活
            if not self._email_jwt:
                self._email_jwt = await self._recover_email_jwt()
            if not self._email_jwt:
                return False

            email_service = EmailService()
            async with AsyncSession(impersonate=IMPERSONATE, timeout=60, proxy=PROXY or None) as session:
                otp_request = await self._step_send_email(session, self.email)
                if not otp_request:
                    return False

                self.otp = await self._wait_for_otp(email_service, self._email_jwt, self.email)
                if not self.otp:
                    return False

                user_id = await self._step_nextauth_login(session, self.email, self.otp)
                if not user_id:
                    return False

                self.acc.cookie_str = _cookies_dict_to_str(self._cookies)
                self.acc.user_id = user_id
                self.acc.healthy = True
                self.acc.disabled = False
                self.acc.fail_count = 0
                return True
        except Exception as e:
            log.error(f"CosverseRefresher 异常: {e}")
            return False


# ── 对话客户端 ────────────────────────────────────────────

def messages_to_text(messages: list[dict]) -> str:
    parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
        parts.append(f"[{role.capitalize()}]: {content}")
    return "\n".join(parts)


def _build_attachment_list(upload_results: list[dict]) -> list[dict]:
    attachments = []
    for r in upload_results:
        if not r:
            continue
        key = r.get("key", "")
        public_url = r.get("publicUrl", "")
        data_uri = r.get("dataUri", public_url)
        filename = key.split("/")[-1] if key else "file"
        mime, _ = mimetypes.guess_type(filename)
        mime = mime or "application/octet-stream"
        is_image = mime.startswith("image/")
        attachments.append({
            "key": key,
            "publicUrl": public_url,
            "dataUri": data_uri,
            "name": filename,
            "contentType": mime,
            "type": "image" if is_image else "file",
        })
    return attachments


def _chat_body(
    conv_id: str,
    text: str,
    model: str,
    provider: str,
    user_name: str,
    attachments: Optional[list[dict]] = None,
    chat_title: str = "New Chat",
) -> dict:
    user_msg = {
        "role": "user",
        "content": text,
        "parts": [{"type": "text", "text": text}],
    }

    if attachments:
        user_msg["experimental_attachments"] = [
            {
                "url": att.get("dataUri", att.get("publicUrl", "")),
                "filename": att.get("name", "file"),
                "contentType": att.get("contentType", "image/png"),
            }
            for att in attachments
        ]

    return {
        "id": conv_id,
        "messages": [user_msg],
        "model": model,
        "provider": provider,
        "conversationId": conv_id,
        "selectedTools": [],
        "guestMessageCount": 1,
        "userSettings": {"userName": user_name, "occupation": "", "traits": "", "preferences": ""},
        "chatTitle": chat_title,
        "attachments": [],
        "pdfAttachments": [],
    }


async def _fetch_chat_title(
    session: AsyncSession,
    text: str,
    acc: AccountState,
    conv_id: str,
    cfg,
) -> str:
    if not cfg or not getattr(cfg, "title_action", None):
        return "New Chat"
    try:
        router_state = cfg.router_state_chat(conv_id)
        resp = await session.post(
            COSVERSE_WEB,
            headers=_nextjs_headers(cfg.title_action, cfg.deployment_id, router_state),
            data=json.dumps([text]),
            cookies=acc.cookies_dict(),
            timeout=15,
        )
        for line in resp.text.strip().split("\n"):
            if line.startswith("1:"):
                title = json.loads(line[2:])
                if isinstance(title, str) and title:
                    log.info(f"[{acc.email}] chatTitle={title}")
                    return title
    except Exception as e:
        log.warning(f"[{acc.email}] 获取 chatTitle 失败（忽略）: {e}")
    return "New Chat"


async def stream_cosverse_response(
    text: str,
    acc: AccountState,
    model: str = "gemini-3-flash-preview",
    provider: str = "google",
    attachments: Optional[list[dict]] = None,
    conv_id: Optional[str] = None,
    generate_title: bool = True,
) -> AsyncIterator[str]:
    """
    向 Cosverse /api/chat 发送请求并流式返回文本块。
    若响应 HTTP 200 但所有 chunk 内容为空（completion_tokens=0），
    抛出 EmptyResponseError，由调用方触发 refresh + 重试逻辑。
    """
    if conv_id is None:
        conv_id = _random_conv_id()

    cfg = await fetch_action_config()

    async with AsyncSession(impersonate=IMPERSONATE, timeout=120, proxy=PROXY or None) as session:
        chat_title = (
            await _fetch_chat_title(session, text, acc, conv_id, cfg)
            if generate_title else "New Chat"
        )
        body = _chat_body(conv_id, text, model, provider, acc.name or acc.email, attachments, chat_title)

        resp = await session.post(
            f"{COSVERSE_API}/chat",
            headers={
                **COMMON_HEADERS,
                "accept": "*/*",
                "content-type": "application/json",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "referer": f"{COSVERSE_WEB}/",
            },
            json=body,
            cookies=acc.cookies_dict(),
            stream=True,
            timeout=120,
        )
        if resp.status_code == 401:
            raise PermissionError(f"cookie_expired:{acc.email}")
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Cosverse /api/chat 返回 {resp.status_code}")

        yielded_any = False
        async for raw_line in resp.aiter_lines():
            line = (
                raw_line.decode("utf-8", errors="ignore")
                if isinstance(raw_line, bytes)
                else raw_line
            )
            if not line:
                continue
            if line.startswith("0:"):
                chunk = line[2:]
                try:
                    chunk = json.loads(chunk)
                except Exception:
                    pass
                if chunk:
                    yielded_any = True
                    yield chunk
            elif line.startswith("3:"):
                # 服务端应用层错误（如 invalid_grant），与 cookie 无关，直接抛出不重试
                try:
                    err_msg = json.loads(line[2:])
                except Exception:
                    err_msg = line[2:]
                log.error(f"[{acc.email}] 服务端错误: {err_msg}")
                raise ServerError(str(err_msg))
            elif line.startswith("e:") or line.startswith("d:"):
                break

        # HTTP 200 但无任何有效文本块 → 视为 cookie 过期 / 账号异常
        if not yielded_any:
            raise EmptyResponseError(f"empty_response:{acc.email}")


# ── 带重试的对话入口（供 chat router 调用）────────────────

async def stream_with_retry(
    text: str,
    model: str,
    provider: str,
    attachments: Optional[list[dict]] = None,
    conv_id: Optional[str] = None,
) -> AsyncIterator[str]:
    from app.cookie_manager import cm

    _TRANSIENT_ERRORS = (PermissionError, EmptyResponseError)

    async def _try_stream(acc: AccountState) -> AsyncIterator[str]:
        async for chunk in stream_cosverse_response(
            text, acc, model, provider, attachments, conv_id
        ):
            yield chunk

    async def _refresh_and_retry(acc: AccountState) -> AsyncIterator[str]:
        log.warning(f"[{acc.email}] 触发 refresh 后重试...")
        ok = await cm.refresh_account(acc)
        if not ok:
            raise RuntimeError(f"[{acc.email}] refresh 失败，无法重试")
        async for chunk in _try_stream(acc):
            yield chunk

    # ── 第一次尝试 ──────────────────────────────────────
    acc = await cm.get_next()
    try:
        async for chunk in _try_stream(acc):
            yield chunk
        return
    except ServerError:
        raise
    except _TRANSIENT_ERRORS as e:
        log.warning(f"首次请求失败（{type(e).__name__}）: {e}")

    # ── 第二次：对同一账号 refresh 后重试 ───────────────
    try:
        async for chunk in _refresh_and_retry(acc):
            yield chunk
        return
    except ServerError:
        raise
    except Exception as e:
        log.warning(f"refresh 后重试仍失败: {e}")

    # ── 第三次：换备用账号 ──────────────────────────────
    try:
        acc2 = await cm.mark_failed_and_get_replacement(acc, reason="empty_or_expired", attempt=2)
        async for chunk in _try_stream(acc2):
            yield chunk
        return
    except ServerError:
        raise
    except _TRANSIENT_ERRORS as e:
        log.warning(f"备用账号（{acc2.email}）也失败: {e}")
    except Exception as e:
        log.warning(f"切换备用账号异常: {e}")

    # ── 第四次：注册新账号 ──────────────────────────────
    log.warning("两个账号均失败，尝试注册新账号...")
    try:
        acc3 = await cm.mark_failed_and_get_replacement(acc, reason="all_failed", attempt=3)
        async for chunk in _try_stream(acc3):
            yield chunk
        return
    except Exception as e:
        log.error(f"注册新账号后仍失败: {e}")
        raise RuntimeError("所有重试策略均已耗尽，对话请求失败") from e
