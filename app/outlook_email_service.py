import asyncio
import imaplib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email import message_from_string
from pathlib import Path
from typing import Optional
import quopri
import requests

from app.config import PROXY, OUTLOOK_FILE, OUTLOOK_POINTER_FILE

log = logging.getLogger("cosverse_proxy.outlook")


@dataclass
class OutlookAccount:
    address: str
    password: str
    client_id: str
    refresh_token: str
    index: int


def get_outlook_access_token(client_id: str, refresh_token: str, retries: int = 4) -> str:
    url = os.getenv("OUTLOOK_TOKEN_URL", "https://login.microsoftonline.com/common/oauth2/v2.0/token")
    proxies = {"http": PROXY, "https": PROXY} if PROXY else None
    last_exc = None

    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(
                url,
                data={
                    "client_id": client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                proxies=proxies,
                timeout=25,
            )
            resp.raise_for_status()
            data = resp.json()
            if "access_token" in data:
                return data["access_token"]
        except Exception as e:
            last_exc = e
            if attempt < retries:
                time.sleep(1.5 * attempt)

    raise RuntimeError(f"Failed to obtain Outlook access token: {last_exc}")


def get_folder_highest_uids(
    email: str, client_id: str, refresh_token: str, access_token: str = None
) -> dict[str, int]:
    result = {'"Junk"': 0, "INBOX": 0}
    try:
        token = access_token or get_outlook_access_token(client_id, refresh_token)
    except Exception as e:
        log.warning(f"[{email}] 获取基线 UID 时换取 token 失败: {e}")
        return result

    hosts = [
        h.strip()
        for h in os.getenv(
            "OUTLOOK_IMAP_HOSTS", "outlook.office365.com;imap-mail.outlook.com"
        )
        .replace(",", ";")
        .split(";")
        if h.strip()
    ]
    port = int(os.getenv("OUTLOOK_IMAP_PORT", "993"))

    for host in hosts:
        c = None
        try:
            c = imaplib.IMAP4_SSL(host, port, timeout=25)
            auth = f"user={email}\x01auth=Bearer {token}\x01\x01".encode()
            typ, _ = c.authenticate("XOAUTH2", lambda _: auth)
            if typ != "OK":
                continue

            for folder in ['"Junk"', "INBOX"]:
                try:
                    typ, _ = c.select(folder, readonly=True)
                    if typ == "OK":
                        typ, ids = c.search(None, "ALL")
                        if typ == "OK" and ids[0].split():
                            result[folder] = int(ids[0].split()[-1])
                except Exception:
                    pass
            return result
        except Exception:
            pass
        finally:
            if c:
                try:
                    c.logout()
                except Exception:
                    pass

    return result


def extract_cosverse_otp(raw_email: str) -> Optional[str]:
    """从邮件原始内容中精准提取 Cosverse 验证码"""
    if not raw_email:
        return None

    try:
        msg = message_from_string(raw_email)
        bodies = []
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype in ("text/plain", "text/html"):
                    p = part.get_payload(decode=True)
                    if p:
                        charset = part.get_content_charset() or "utf-8"
                        bodies.append(p.decode(charset, errors="ignore"))
        else:
            p = msg.get_payload(decode=True)
            if p:
                charset = msg.get_content_charset() or "utf-8"
                bodies.append(p.decode(charset, errors="ignore"))
            else:
                raw_p = msg.get_payload()
                if isinstance(raw_p, str):
                    bodies.append(raw_p)

        for body in bodies:
            # 1. 匹配 HTML 标签内数字: Verification code</p>...<p...>295292</p>
            m = re.search(
                r"verification\s+code</p>\s*<p[^>]*>\s*([0-9]{6})\s*</p>",
                body,
                re.IGNORECASE,
            )
            if m:
                return m.group(1)

            # 2. 匹配纯文本结构: Verification code \n\n 295292
            m = re.search(
                r"verification\s+code\s*\r?\n+\s*([0-9]{6})", body, re.IGNORECASE
            )
            if m:
                return m.group(1)

            # 3. 宽松文本匹配
            m = re.search(
                r"verification\s+code[\s\S]{0,100}?([0-9]{6})", body, re.IGNORECASE
            )
            if m:
                return m.group(1)

    except Exception as e:
        log.warning(f"解析邮件验证码异常: {e}")

    return None


def read_outlook_verification_code_with_token(
    email: str, access_token: str, baseline_folders: dict[str, int] = None
) -> Optional[str]:
    baseline = baseline_folders or {'"Junk"': 0, "INBOX": 0}
    hosts = [
        h.strip()
        for h in os.getenv(
            "OUTLOOK_IMAP_HOSTS", "outlook.office365.com;imap-mail.outlook.com"
        )
        .replace(",", ";")
        .split(";")
        if h.strip()
    ]
    port = int(os.getenv("OUTLOOK_IMAP_PORT", "993"))

    for host in hosts:
        c = None
        try:
            c = imaplib.IMAP4_SSL(host, port, timeout=25)
            auth = f"user={email}\x01auth=Bearer {access_token}\x01\x01".encode()
            typ, _ = c.authenticate("XOAUTH2", lambda _: auth)
            if typ != "OK":
                continue

            # 邮件通常进入 INBOX 或 Junk
            for folder in ["INBOX", '"Junk"']:
                try:
                    typ, _ = c.select(folder, readonly=True)
                    if typ != "OK":
                        continue
                    typ, ids = c.search(None, "ALL")
                    if typ != "OK" or not ids[0].split():
                        continue

                    all_ids = [int(mid) for mid in ids[0].split()]
                    after_id = baseline.get(folder, 0)
                    candidates = [mid for mid in all_ids if mid > after_id]
                    if not candidates and after_id == 0:
                        candidates = all_ids[-1:]

                    for mid in candidates[::-1][:3]:
                        typ, data = c.fetch(str(mid).encode(), "(RFC822)")
                        if typ != "OK":
                            continue
                        raw_bytes = b"".join(
                            x[1] for x in data if isinstance(x, tuple)
                        )
                        try:
                            decoded = quopri.decodestring(raw_bytes).decode(
                                "utf-8", "ignore"
                            )
                        except Exception:
                            decoded = raw_bytes.decode("utf-8", "ignore")

                        if "cosverse" in decoded.lower():
                            code = extract_cosverse_otp(decoded)
                            if code:
                                return code
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            if c:
                try:
                    c.logout()
                except Exception:
                    pass

    return None


async def wait_for_outlook_code(
    email: str,
    client_id: str,
    refresh_token: str,
    baseline_folders: dict[str, int] = None,
    timeout: int = 90,
    interval: int = 4,
) -> str:
    access_token = await asyncio.to_thread(
        get_outlook_access_token, client_id, refresh_token
    )

    for _ in range(max(1, timeout // interval)):
        await asyncio.sleep(interval)
        try:
            code = await asyncio.to_thread(
                read_outlook_verification_code_with_token,
                email,
                access_token,
                baseline_folders,
            )
            if code:
                return code
        except Exception:
            pass

    raise TimeoutError(f"Outlook verification code timeout: {email}")


class OutlookAccountPool:
    """管理 outlook_live_...txt 账号池，支持：
    1. 状态持久化（已用账号、重试队列、失效账号、新号游标）
    2. 历史遗漏账号自愈（自动拾回因临时网络波动跳过的账号，绝不浪费）
    3. 优先级取号（优先重试临时网络波动未成功的账号，其次扫描新号，不单纯用递增 index）
    4. 两阶段确认机制（mark_success / mark_failed，网络失败安全放回重试池）
    """

    def __init__(
        self,
        account_file: Path = OUTLOOK_FILE,
        pointer_file: Path = OUTLOOK_POINTER_FILE,
    ):
        self.account_file = Path(account_file)
        self.pointer_file = Path(pointer_file)
        self.accounts: list[tuple[str, str, str, str]] = []
        self.email_to_account: dict[str, tuple[str, str, str, str, int]] = {}
        self.current_index: int = 0
        self.used_emails: set[str] = set()
        self.retry_queue: dict[str, dict] = {}
        self.invalid_emails: dict[str, dict] = {}
        self.in_flight: set[str] = set()
        self._lock = asyncio.Lock()
        
        self._load_accounts()
        self._load_state()

    def _load_accounts(self) -> None:
        if not self.account_file.exists():
            raise FileNotFoundError(
                f"Outlook account file not found: {self.account_file}"
            )
        accs = []
        with open(self.account_file, encoding="utf-8-sig") as f:
            for line in f:
                parts = line.strip().split("----", 3)
                if len(parts) == 4:
                    email = parts[0].strip()
                    pwd = parts[1].strip()
                    cid = parts[2].strip()
                    rtok = parts[3].strip()
                    idx = len(accs)
                    accs.append((email, pwd, cid, rtok))
                    self.email_to_account[email.lower()] = (email, pwd, cid, rtok, idx)
        if not accs:
            raise ValueError(f"Outlook account file is empty: {self.account_file}")
        self.accounts = accs
        log.info(f"Loaded {len(self.accounts)} accounts from {self.account_file}")

    def _sync_with_tokens_file(self) -> None:
        """从 cosverse_tokens.json 同步当前已经在使用的 Outlook 账号"""
        from app.config import TOKENS_FILE
        if TOKENS_FILE.exists():
            try:
                data = json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
                acc_list = data.get("accounts", []) if isinstance(data, dict) else data
                for a in acc_list:
                    em = a.get("email", "").strip().lower()
                    if em and "@outlook" in em:
                        self.used_emails.add(em)
            except Exception as e:
                log.warning(f"同步 tokens 文件账号失败: {e}")

    def _load_state(self) -> None:
        if self.pointer_file.exists():
            try:
                data = json.loads(self.pointer_file.read_text(encoding="utf-8"))
                self.current_index = int(data.get("current_index", 0))
                self.used_emails = set(e.lower() for e in data.get("used_emails", []))
                self.retry_queue = {k.lower(): v for k, v in data.get("retry_queue", {}).items()}
                self.invalid_emails = {k.lower(): v for k, v in data.get("invalid_emails", {}).items()}
            except Exception as e:
                log.warning(f"Failed to read pointer file: {e}")
                self.current_index = 0

        # 同步 tokens 文件中已经在用的账号
        self._sync_with_tokens_file()

        # 历史遗漏账号自愈扫描：
        # 扫描从 0 到 current_index 之间因过去网络波动或中断被跳过的账号，自动收纳至待重试队列，绝不浪费
        recovered = 0
        limit_scan = min(self.current_index, len(self.accounts))
        for i in range(limit_scan):
            em = self.accounts[i][0].strip().lower()
            if em not in self.used_emails and em not in self.invalid_emails and em not in self.retry_queue:
                self.retry_queue[em] = {
                    "fail_count": 0,
                    "last_error": "previously_skipped_by_index",
                    "last_try": 0,
                }
                recovered += 1

        if recovered > 0:
            log.info(f"♻️ [Outlook] 自动拾回历史因网络波动/跳过而未使用的账号 {recovered} 个进入重试待选池")
            self._save_state()

    def _save_state(self, last_email: str = "") -> None:
        try:
            data = {
                "current_index": self.current_index,
                "last_email": last_email,
                "total_accounts": len(self.accounts),
                "used_emails": sorted(list(self.used_emails)),
                "retry_queue": self.retry_queue,
                "invalid_emails": self.invalid_emails,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self.pointer_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            log.warning(f"Failed to save pointer file: {e}")

    async def get_next_account(self) -> OutlookAccount:
        """
        智能获取下一个可用 Outlook 账号：
        1. 优先从 retry_queue（因网络抖动未成功的账号）中取出重试，避免账号浪费；
        2. 其次顺着游标扫描尚未被使用的新账号；
        3. 两阶段确认：取出时占位防并发，完成或失败后通知 pool。
        """
        async with self._lock:
            now = time.time()

            # 优先级 1: 检查重试队列中因网络波动失败的账号
            # 规则：未在处理中，重试未超 5 次，冷却时间超过 15 秒
            for em, info in list(self.retry_queue.items()):
                if em in self.in_flight:
                    continue
                fail_cnt = info.get("fail_count", 0)
                last_try = info.get("last_try", 0)
                if fail_cnt < 5 and (now - last_try >= 15):
                    raw = self.email_to_account.get(em)
                    if raw:
                        self.in_flight.add(em)
                        email, pwd, cid, rtok, idx = raw
                        log.info(
                            f"🔄 [Outlook] 拾回/重试因网络波动未成功的账号: {email} "
                            f"(重试第 {fail_cnt + 1} 次，上次原因: {info.get('last_error', 'unknown')})"
                        )
                        return OutlookAccount(
                            address=email,
                            password=pwd,
                            client_id=cid,
                            refresh_token=rtok,
                            index=idx,
                        )

            # 优先级 2: 从游标扫描全新的未用账号
            total = len(self.accounts)
            for _ in range(total):
                curr_idx = self.current_index % total
                email, pwd, cid, rtok = self.accounts[curr_idx]
                em = email.lower()
                self.current_index = (curr_idx + 1) % total

                if (
                    em in self.used_emails
                    or em in self.invalid_emails
                    or em in self.retry_queue
                    or em in self.in_flight
                ):
                    # 已用或已在队列中，继续向前扫描
                    continue

                # 找到未用新账号
                self.in_flight.add(em)
                self._save_state(last_email=email)
                log.info(f"🆕 [Outlook] 选取全新账号: {email} (游标推进至 {self.current_index})")
                return OutlookAccount(
                    address=email,
                    password=pwd,
                    client_id=cid,
                    refresh_token=rtok,
                    index=curr_idx,
                )

            # 优先级 3: 兜底情况（全池账号均已尝试过），从 retry_queue 选取重试次数最多的账号再试一次
            for em in self.retry_queue:
                if em not in self.in_flight:
                    raw = self.email_to_account.get(em)
                    if raw:
                        self.in_flight.add(em)
                        email, pwd, cid, rtok, idx = raw
                        log.warning(f"⚠️ [Outlook] 全池扫描完毕，兜底复用重试账号: {email}")
                        return OutlookAccount(
                            address=email,
                            password=pwd,
                            client_id=cid,
                            refresh_token=rtok,
                            index=idx,
                        )

            # 若完全无可取账号，抛出明确异常
            raise RuntimeError("Outlook 账号池中所有账号均已使用或正在处理中，无可用的候选账号")

    async def mark_success(self, email: str) -> None:
        """账号注册/激活成功确认：登记为已用并移出重试池"""
        async with self._lock:
            em = email.lower()
            self.in_flight.discard(em)
            self.used_emails.add(em)
            self.retry_queue.pop(em, None)
            self._save_state(last_email=email)
            log.info(f"✅ [Outlook] 账号成功登记为可用: {email} (已从待重试池移除)")

    async def mark_failed(self, email: str, error: str, is_permanent: bool = False) -> None:
        """账号注册/激活失败反馈：临时网络错误安全放回重试池，绝不浪费丢号"""
        async with self._lock:
            em = email.lower()
            self.in_flight.discard(em)
            if is_permanent:
                self.invalid_emails[em] = {
                    "reason": error,
                    "marked_at": datetime.now(timezone.utc).isoformat(),
                }
                self.retry_queue.pop(em, None)
                log.warning(f"🚫 [Outlook] 账号已被微软/认证明确拒绝，标记为永久失效: {email} (原因: {error})")
            else:
                info = self.retry_queue.get(em, {"fail_count": 0})
                info["fail_count"] = info.get("fail_count", 0) + 1
                info["last_error"] = str(error)[:200]
                info["last_try"] = time.time()
                self.retry_queue[em] = info
                log.warning(
                    f"⚠️ [Outlook] 账号因临时网络波动未成功，已安全保留在重试队列中: {email} "
                    f"(累计重试 {info['fail_count']} 次, 原因: {error})"
                )
            self._save_state(last_email=email)

    async def get_account_by_email(self, email: str) -> Optional[OutlookAccount]:
        raw = self.email_to_account.get(email.lower())
        if raw:
            return OutlookAccount(
                address=raw[0],
                password=raw[1],
                client_id=raw[2],
                refresh_token=raw[3],
                index=raw[4],
            )
        return None


# 单例管理
_pool_singleton: Optional[OutlookAccountPool] = None

def get_outlook_pool() -> OutlookAccountPool:
    global _pool_singleton
    if _pool_singleton is None:
        _pool_singleton = OutlookAccountPool()
    return _pool_singleton
