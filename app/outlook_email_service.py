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
    """管理 outlook_live_...txt 账号池，持久化当前指针，支持循环轮询"""

    def __init__(
        self,
        account_file: Path = OUTLOOK_FILE,
        pointer_file: Path = OUTLOOK_POINTER_FILE,
    ):
        self.account_file = Path(account_file)
        self.pointer_file = Path(pointer_file)
        self.accounts: list[tuple[str, str, str, str]] = []
        self._lock = asyncio.Lock()
        self._load_accounts()

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
                    accs.append((parts[0].strip(), parts[1].strip(), parts[2].strip(), parts[3].strip()))
        if not accs:
            raise ValueError(f"Outlook account file is empty: {self.account_file}")
        self.accounts = accs
        log.info(f"Loaded {len(self.accounts)} accounts from {self.account_file}")

    def load_pointer(self) -> int:
        if self.pointer_file.exists():
            try:
                data = json.loads(self.pointer_file.read_text(encoding="utf-8"))
                return int(data.get("current_index", 0))
            except Exception as e:
                log.warning(f"Failed to read pointer file: {e}")
        return 0

    def save_pointer(self, index: int, last_email: str = "") -> None:
        try:
            data = {
                "current_index": index,
                "last_email": last_email,
                "total_accounts": len(self.accounts),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self.pointer_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            log.warning(f"Failed to save pointer file: {e}")

    async def get_next_account(self) -> OutlookAccount:
        """获取下一个 Outlook 账号并递增指针，指针在 0 ~ total-1 之间循环"""
        async with self._lock:
            curr_idx = self.load_pointer()
            if curr_idx >= len(self.accounts):
                curr_idx = 0
            
            email, pwd, cid, rtok = self.accounts[curr_idx]
            next_idx = (curr_idx + 1) % len(self.accounts)
            self.save_pointer(next_idx, email)
            return OutlookAccount(
                address=email,
                password=pwd,
                client_id=cid,
                refresh_token=rtok,
                index=curr_idx,
            )

    async def get_account_by_email(self, email: str) -> Optional[OutlookAccount]:
        for i, acc in enumerate(self.accounts):
            if acc[0].lower() == email.lower():
                return OutlookAccount(
                    address=acc[0],
                    password=acc[1],
                    client_id=acc[2],
                    refresh_token=acc[3],
                    index=i,
                )
        return None
