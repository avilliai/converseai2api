# app/models.py

ACCOUNT_QUOTA = 100  # 默认 100 次，根据 /usage 实时同步


class AccountState:
    def __init__(self, d: dict, index: int = 1):
        self.index = index
        self.email = d.get("email", "")
        self.name = d.get("name", "")
        self.cookie_str = d.get("cookie_str", "")
        self.user_id = d.get("user_id", "")
        self.disabled = d.get("disabled", False)
        self.healthy = d.get("healthy", True)
        self.usage_count = d.get("usage_count", 0)
        self.message_limit = d.get("message_limit", ACCOUNT_QUOTA)
        self.message_usage = d.get("message_usage", self.usage_count)
        self.quota_exhausted_flag = d.get("quota_exhausted", False)
        self.fail_count = d.get("fail_count", 0)
        
        # Outlook 凭据（用于重新登录/保活和指针恢复）
        self.password = d.get("password", "")
        self.client_id = d.get("client_id", "")
        self.refresh_token = d.get("refresh_token", "")
        self.email_jwt = d.get("email_jwt", "")

    @property
    def remaining(self) -> int:
        """剩余额度"""
        limit = self.message_limit if self.message_limit > 0 else ACCOUNT_QUOTA
        return max(0, limit - self.message_usage)

    @property
    def is_alive(self) -> bool:
        """账号当前是否可用（健康、未禁用、且配额未耗尽）"""
        return self.healthy and not self.disabled and not self.quota_exhausted

    @property
    def quota_exhausted(self) -> bool:
        """配额是否已耗尽"""
        if self.quota_exhausted_flag:
            return True
        limit = self.message_limit if self.message_limit > 0 else ACCOUNT_QUOTA
        return self.message_usage >= limit

    def mark_quota_exhausted(self) -> None:
        """标记配额耗尽并设为不可用，保留在账号列表中不删除"""
        self.quota_exhausted_flag = True
        self.disabled = True

    def mark_quota_recovered(self) -> None:
        """配额重置后重新启用"""
        self.quota_exhausted_flag = False
        self.disabled = False
        self.healthy = True
        self.fail_count = 0

    @property
    def token_hint(self) -> str:
        """从 cookie 中截取 session-token，作为 Token 摘要"""
        if not self.cookie_str:
            return "N/A"
        for part in self.cookie_str.split(";"):
            part = part.strip()
            if part.startswith("__Secure-authjs.session-token="):
                val = part.split("=", 1)[1]
                if len(val) > 20:
                    return f"{val[:10]}...{val[-10:]}"
                return val
        return "N/A"

    def cookies_dict(self) -> dict:
        """将 cookie 字符串解析为 dict 格式"""
        if not self.cookie_str:
            return {}
        res = {}
        for part in self.cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                res[k.strip()] = v.strip()
        return res

    def to_dict(self) -> dict:
        d = {
            "email": self.email,
            "name": self.name,
            "cookie_str": self.cookie_str,
            "user_id": self.user_id,
            "disabled": self.disabled,
            "healthy": self.healthy,
            "usage_count": self.usage_count,
            "message_limit": self.message_limit,
            "message_usage": self.message_usage,
            "quota_exhausted": self.quota_exhausted_flag,
            "fail_count": self.fail_count,
        }
        if self.password:
            d["password"] = self.password
        if self.client_id:
            d["client_id"] = self.client_id
        if self.refresh_token:
            d["refresh_token"] = self.refresh_token
        if self.email_jwt:
            d["email_jwt"] = self.email_jwt
        return d
