# app/cookie_manager.py

import asyncio
import json
import logging
import os
import random
import threading
from typing import Optional

import dotenv
from fastapi import HTTPException

from app.config import TOKENS_FILE
from app.models import AccountState
from app.usage_client import fetch_usage

log = logging.getLogger("cosverse_proxy.cookie_manager")

PRE_REGISTER_THRESHOLD = 2
ACCOUNT_MAX_USAGE = 100
dotenv.load_dotenv()
# ── 动态账号池配置 ──────────────────────────────────────────
# 不再对全量账号做批量保活（高频探活会触发验证码风控）。
# 改为只维护一个小的"活跃池"，仅对池内账号做探活/保活；
# 池内账号配额耗尽或连续失败达到阈值时被请出池子，
# 再从备用账号（健康但不在池内）或新注册账号中补充。
ACTIVE_POOL_SIZE = int(os.environ.get("ACTIVE_POOL_SIZE", "3"))
REPLENISH_BATCH = int(os.environ.get("REPLENISH_BATCH", "3"))
FAIL_THRESHOLD = int(os.environ.get("ACCOUNT_FAIL_THRESHOLD", "3"))
REPLENISH_PROBE_LIMIT = int(os.environ.get("REPLENISH_PROBE_LIMIT", "20"))
AUTO_OTP_REFRESH = os.environ.get("AUTO_OTP_REFRESH", "false").lower() in ("1", "true", "yes")
AUTO_OTP_REFRESH_MAX_PER_TICK = int(
    os.environ.get("AUTO_OTP_REFRESH_MAX_PER_TICK", "1")
)
AUTO_REGISTER_ON_REPLENISH = os.environ.get(
    "AUTO_REGISTER_ON_REPLENISH", "false"
).lower() in ("1", "true", "yes")
print(AUTO_REGISTER_ON_REPLENISH)


class CookieManager:
    def __init__(self):
        self.accounts: list[AccountState] = []
        self._idx: int = 0
        self._async_lock = asyncio.Lock()  # 仅保护换号/注册等写操作
        self._idx_lock = asyncio.Lock()  # 仅保护 _idx 轮询，极短临界区
        self._sync_lock = threading.Lock()
        self._registering = False

        # ── 动态账号池 ──────────────────────────────────────
        self.active_pool: list[int] = []  # 活跃账号的 index 列表
        self._pool_lock = asyncio.Lock()  # 保护活跃池增删
        self._replenishing = False  # 防止补号任务并发重入

    # ── 持久化 ────────────────────────────────────────────
    def load(self) -> None:
        if not TOKENS_FILE.exists():
            log.warning(f"未找到 {TOKENS_FILE}，已初始化空列表")
            self.save()
            return
        try:
            data = json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
            with self._sync_lock:
                accounts = []
                for i, d in enumerate(data):
                    if not d.get("cookie_str"):
                        continue
                    acc = AccountState(d, i + 1)
                    if d.get("email_jwt"):
                        acc.email_jwt = d["email_jwt"]
                    acc.fail_count = d.get("fail_count", 0)
                    accounts.append(acc)
                self.accounts = accounts
            log.info(f"✅ 成功加载账号: {len(self.accounts)} 个")

            # 恢复活跃池：优先使用持久化的 active_pool 字段；
            # 若文件中没有（旧版本数据/首次升级），则用启发式规则重建。
            pool_file = TOKENS_FILE.with_name("active_pool.json")
            restored_pool: list[int] = []
            if pool_file.exists():
                try:
                    restored_pool = json.loads(pool_file.read_text(encoding="utf-8"))
                except Exception as e:
                    log.warning(f"解析 active_pool.json 失败: {e}")

            valid_indices = {a.index for a in self.accounts}
            self.active_pool = [
                idx for idx in restored_pool
                if idx in valid_indices
                   and self._account_is_eligible(self._find_by_index(idx))
            ]

            if not self.active_pool:
                # 没有可恢复的活跃池快照。注意：这些备用账号的 cookie 默认视为
                # 已过期/陈旧（哪怕 healthy 标记为 True，那也只是上次保存时的状态，
                # 不代表当前真的可用）。load() 是同步方法、且在服务启动阶段被调用，
                # 不适合在这里同步阻塞地逐个收验证码邮件激活。
                # 因此这里不直接把账号塞进 active_pool，而是保持空池，
                # 由 main.py 启动后调用的 _replenish_active_pool() 异步任务
                # 统一走 usage 余额探测成功才入池。
                log.info("♻️ 未找到可用的活跃池快照，活跃池将在启动后异步激活补充")
            else:
                log.info(f"✅ 已恢复活跃池: {len(self.active_pool)} 个账号 {self.active_pool}")
        except Exception as e:
            log.error(f"解析 tokens 失败: {e}")

    def save(self) -> None:
        with self._sync_lock:
            try:
                raw = []
                for acc in self.accounts:
                    d = acc.to_dict()
                    # email_jwt는 to_dict()에 없을 수 있으므로 명시적으로 추가
                    email_jwt = getattr(acc, "email_jwt", "")
                    if email_jwt:
                        d["email_jwt"] = email_jwt
                    # fail_count：连续失败次数（用于活跃池淘汰判断）
                    fail_count = getattr(acc, "fail_count", 0)
                    if fail_count:
                        d["fail_count"] = fail_count
                    raw.append(d)
                TOKENS_FILE.write_text(
                    json.dumps(raw, ensure_ascii=False, indent=4), encoding="utf-8"
                )
                # 活跃池快照单独存一份小文件，避免污染 tokens.json 结构
                try:
                    pool_file = TOKENS_FILE.with_name("active_pool.json")
                    pool_file.write_text(
                        json.dumps(self.active_pool, ensure_ascii=False), encoding="utf-8"
                    )
                except Exception as e:
                    log.warning(f"保存 active_pool 快照失败: {e}")
            except Exception as e:
                log.warning(f"保存 tokens 失败: {e}")

    async def async_save(self) -> None:
        await asyncio.to_thread(self.save)

    # ── 提供给管理后台接口的数据 ──────────────────────────
    def get_tokens_detail(self) -> dict:
        tokens_list = []
        total_remaining = 0
        alive_count = 0

        with self._sync_lock:
            pool_set = set(self.active_pool)
            for acc in self.accounts:
                remaining = acc.remaining
                total_remaining += remaining

                if acc.healthy and not acc.disabled and remaining > 0:
                    alive_count += 1

                tokens_list.append({
                    "index": acc.index,
                    "email": acc.email,
                    "token_hint": getattr(acc, "token_hint", acc.email),
                    "is_alive": acc.healthy,
                    "disabled": acc.disabled,
                    "remaining": remaining,
                    "in_active_pool": acc.index in pool_set,
                    "fail_count": getattr(acc, "fail_count", 0),
                })
            pool_size = len(self.active_pool)

        return {
            "tokens": tokens_list,
            "summary": {
                "total": len(self.accounts),
                "alive": alive_count,
                "total_remaining": total_remaining,
                "active_pool_size": pool_size,
                "active_pool_target": ACTIVE_POOL_SIZE,
                "last_refresh": None,
            },
        }

    # ── 账号增删 ──────────────────────────────────────────
    def add_account(self, data: dict) -> bool:
        with self._sync_lock:
            for acc in self.accounts:
                if acc.email == data.get("email"):
                    acc.cookie_str = data.get("cookie_str", acc.cookie_str)
                    acc.name = data.get("name", acc.name)
                    acc.user_id = data.get("user_id", acc.user_id)
                    acc.healthy = True
                    acc.disabled = False
                    acc.fail_count = 0
                    acc.usage_count = data.get("usage_count", acc.usage_count)
                    acc.message_limit = data.get("message_limit", acc.message_limit)
                    acc.message_usage = data.get("message_usage", acc.message_usage)
                    acc.quota_exhausted_flag = data.get("quota_exhausted", False)
                    if data.get("password"):
                        acc.password = data["password"]
                    if data.get("client_id"):
                        acc.client_id = data["client_id"]
                    if data.get("refresh_token"):
                        acc.refresh_token = data["refresh_token"]
                    if data.get("email_jwt"):
                        acc.email_jwt = data["email_jwt"]
                    return False
            new_idx = max([a.index for a in self.accounts] + [0]) + 1
            self.accounts.append(AccountState(data, new_idx))
        return True

    # ── 动态账号池：辅助方法 ──────────────────────────────
    def _find_by_index(self, idx: int) -> Optional[AccountState]:
        return next((a for a in self.accounts if a.index == idx), None)

    def _account_is_eligible(self, acc: Optional[AccountState]) -> bool:
        """账号是否具备进入活跃池的资格：健康、未禁用、未超额、未达连续失败阈值。"""
        if acc is None:
            return False
        if not acc.healthy or acc.disabled:
            return False
        if acc.quota_exhausted:
            return False
        if getattr(acc, "fail_count", 0) >= FAIL_THRESHOLD:
            return False
        return True

    def _discard_account(self, acc: AccountState, reason: str) -> bool:
        """配额每月重置，不永久删除账号，仅移出活跃池并标记保留数据。"""
        acc.healthy = False
        acc.disabled = True
        with self._sync_lock:
            self.active_pool = [
                idx for idx in self.active_pool if idx != acc.index
            ]
        self.save()
        log.info(f"⏸️ [{acc.email}] 已移出活跃池（保留账号数据以备重置），原因: {reason}")
        return True

    async def _evict_from_pool(self, acc: AccountState, reason: str) -> None:
        """将账号请出活跃池（配额耗尽或连续失败），并触发补充。"""
        async with self._pool_lock:
            if acc.index in self.active_pool:
                self.active_pool.remove(acc.index)
                log.info(f"📤 [{acc.email}] 已移出活跃池，原因: {reason}（当前池内 {len(self.active_pool)} 个）")
                await self.async_save()
        asyncio.create_task(self._replenish_active_pool())

    async def _replenish_active_pool(self) -> None:
        """
        补充活跃池至 ACTIVE_POOL_SIZE：
        - 备用账号先调用 usage 余额接口探测，成功后直接加入活跃池。
        - 默认不自动发送 OTP、不自动注册，避免几百账号集中触发邮箱风控。
        - 如显式开启 AUTO_OTP_REFRESH / AUTO_REGISTER_ON_REPLENISH，
          仍会受到全局 OTP 限流与熔断保护。
        防止并发重入：同一时刻只允许一个补充任务运行。
        """
        if self._replenishing:
            return
        self._replenishing = True
        try:
            # 锁内只做"选候选名单"的快照，不在锁内做网络请求（refresh 涉及
            # 收验证码邮件，耗时较长，长时间持锁会阻塞其他 evict/get_next 调用）
            async with self._pool_lock:
                need = ACTIVE_POOL_SIZE - len(self.active_pool)
                if need <= 0:
                    return
                pool_set = set(self.active_pool)
                candidates = [
                    a for a in self.accounts
                    if a.index not in pool_set and self._account_is_eligible(a)
                ][:max(1, REPLENISH_PROBE_LIMIT)]

            activated: list[AccountState] = []
            otp_refreshes = 0
            for acc in candidates:
                if need - len(activated) <= 0:
                    break
                log.info(f"👋 备用账号先执行 usage 余额探测: {acc.email}")
                ok = await self.check_session(acc, save=False)
                if (
                        not ok
                        and AUTO_OTP_REFRESH
                        and otp_refreshes < AUTO_OTP_REFRESH_MAX_PER_TICK
                ):
                    otp_refreshes += 1
                    log.warning(f"🔑 usage 余额探测失败，受控 OTP 刷新: {acc.email}")
                    ok = await self.refresh_account(acc)
                if ok:
                    acc.fail_count = 0
                    activated.append(acc)
                    log.info(f"✅ 备用账号激活成功，准备加入活跃池: {acc.email}")
                else:
                    log.warning(f"⚠️ 备用账号激活失败，跳过: {acc.email}")
                await asyncio.sleep(0.75)

            if activated:
                async with self._pool_lock:
                    for a in activated:
                        if a.index not in self.active_pool:
                            self.active_pool.append(a.index)
                    await self.async_save()
                log.info(f"📥 从备用池补充 {len(activated)} 个激活成功的账号进活跃池: {[a.email for a in activated]}")

            need -= len(activated)

            # 备用池不够（或全部激活失败），注册新账号补足缺口（每批最多 REPLENISH_BATCH 个）
            to_register = (
                min(need, REPLENISH_BATCH)
                if need > 0 and AUTO_REGISTER_ON_REPLENISH else 0
            )
            if to_register > 0:
                log.info(f"🆕 备用池不足/激活失败过多，需注册 {to_register} 个新账号补充活跃池")
                for _ in range(to_register):
                    new_acc = await self._register_new_account()
                    if new_acc:
                        async with self._pool_lock:
                            if new_acc.index not in self.active_pool:
                                self.active_pool.append(new_acc.index)
                                await self.async_save()
                        log.info(f"📥 新注册账号已加入活跃池: {new_acc.email}")
                    else:
                        log.warning("⚠️ 注册新账号失败，活跃池补充未完全达标")
            elif need > 0:
                log.warning(
                    f"活跃池仍缺 {need} 个账号；自动注册已关闭，"
                    "不会触发额外 OTP 邮件"
                )

            # 活跃池补充完成后，顺带检查并触发备用池的后台平滑补充
            await self._ensure_spare_accounts()

        finally:
            self._replenishing = False

    async def _register_new_account(self) -> Optional[AccountState]:
        try:
            from app.cosverse_client import CosverseRegister
            log.info("🔄 开始注册新账号...")
            result = await asyncio.wait_for(CosverseRegister().run(), timeout=180)
            if result:
                self.add_account(result)
                self.save()
                log.info(f"🎉 注册成功: {result['email']}")
                with self._sync_lock:
                    new_acc = next(
                        (a for a in self.accounts if a.email == result["email"]), None
                    )
                return new_acc
            log.warning("💥 注册失败：CosverseRegister.run() 返回 None")
        except Exception as e:
            log.error(f"注册异常: {e}")
        return None

    async def force_register_new_account(self) -> Optional[AccountState]:
        return await self._register_new_account()

    async def _ensure_spare_accounts(self) -> None:
        """后台预注册：确保可用账号总数始终等于活跃池的 2 倍"""
        if not AUTO_REGISTER_ON_REPLENISH:
            return
        if self._registering:
            return

        # 目标总数 = 活跃池大小 * 2
        target_count = ACTIVE_POOL_SIZE * 2

        # 统计未耗尽可用额度的账号数量（包括活跃池 + 备用池）
        valid_count = sum(
            1 for a in self.accounts
            if not a.quota_exhausted
        )

        if valid_count < target_count:
            self._registering = True
            log.info(f"🔄 触发备用池补充：当前总号数 {valid_count}，目标 {target_count}，启动后台平滑注册...")
            asyncio.create_task(self._bg_register(target_count))

    async def _bg_register(self, target_count: int) -> None:
        """平滑后台注册循环：直到数量达标"""
        try:
            while True:
                # 每次循环重新统计，防并发淘汰导致的数据变更
                valid_count = sum(
                    1 for a in self.accounts
                    if not a.quota_exhausted
                )

                need = target_count - valid_count
                if need <= 0:
                    log.info("✅ 账号总池数量已达标 (活跃池*2)，后台注册平滑退出")
                    break

                log.info(f"🆕 后台预注册：还需补充 {need} 个账号 (当前进度 {valid_count}/{target_count})")
                new_acc = await self._register_new_account()

                if not new_acc:
                    log.warning("⚠️ 后台注册失败，暂时中止循环，留待下次探活时重试以避免风控")
                    continue
                    #break

                # 平滑过渡策略：如果还需要继续注册，随机休眠 15~30 秒，避免高频请求被封 IP
                if need > 1:
                    sleep_time = random.uniform(1, 5)
                    log.info(f"⏳ 注册成功，休眠 {sleep_time:.1f} 秒以实现平滑过渡...")
                    await asyncio.sleep(sleep_time)

        except Exception as e:
            log.error(f"后台预注册循环异常: {e}")
        finally:
            self._registering = False

    # ── 保活：单个账号 ────────────────────────────────────
    async def refresh_account(self, acc: AccountState) -> bool:
        """
        对指定账号执行保活（重新登录拿新 cookie）。
        成功后持久化到磁盘。
        """
        from app.cosverse_client import CosverseRefresher
        log.info(f"🔄 开始保活: {acc.email}")
        ok = await CosverseRefresher(acc).run()
        if ok:
            await self.async_save()
            log.info(f"✅ 保活成功并已持久化: {acc.email}")
        else:
            log.warning(f"❌ 保活失败: {acc.email}")
        return ok

    # ── 保活：批量刷新所有不健康账号 ─────────────────────
    async def refresh_all_unhealthy(self) -> dict:
        """
        批量保活所有 healthy=False 或 disabled=True 的账号。
        返回 {"success": [...], "failed": [...]} 统计结果。
        """
        with self._sync_lock:
            targets = [
                a for a in self.accounts
                if not a.healthy or a.disabled
            ]

        log.info(f"🔄 批量保活，目标账号数: {len(targets)}")
        success, failed = [], []

        for acc in targets:
            # 临时解禁，让 Refresher 可以正常写回
            acc.disabled = False
            ok = await self.refresh_account(acc)
            (success if ok else failed).append(acc.email)
            await asyncio.sleep(2)  # 错峰，避免短时间内大量请求

        log.info(f"批量保活完成：成功 {len(success)}，失败 {len(failed)}")
        return {"success": success, "failed": failed}

    # ── 定时保活（供 main.py 后台任务调用）───────────────
    # 注意：保活范围已从"全量账号"收窄为"活跃池"，
    # 且并发度刻意调低、加入错峰间隔，降低触发验证码风控的概率。
    KEEPALIVE_CONCURRENCY = int(os.environ.get("KEEPALIVE_CONCURRENCY", "3"))

    async def keepalive_tick(self) -> None:
        """
        每次定时触发时调用：
        1. 仅对当前活跃池（默认 10 个）内的账号做轻量 LLM 探活
        2. 仅对探活失败的账号执行 refresh（重新登录拿新 cookie）
        3. 探活/刷新仍失败的账号请出活跃池并触发补充

        不再对全量账号做批量探活——高频探活/保活是触发验证码风控的主因。
        全程不持有 _async_lock / _idx_lock，保活期间 chat 请求完全不受阻塞。
        """
        log.info("⏰ keepalive_tick 开始（仅活跃池）")

        with self._sync_lock:
            pool_set = set(self.active_pool)
            candidates = [a for a in self.accounts if a.index in pool_set or a.quota_exhausted]

        if not candidates:
            log.info("⏰ 活跃池为空，跳过探活，尝试补充。")
            await self._replenish_active_pool()
            log.info("⏰ keepalive_tick 完成")
            return

        log.info(f"⏰ 开始对活跃池探活，目标账号数: {len(candidates)}，并发度: {self.KEEPALIVE_CONCURRENCY}")

        sem = asyncio.Semaphore(self.KEEPALIVE_CONCURRENCY)
        probe_counter = {"alive": 0, "dead": 0}
        dead_accounts: list[AccountState] = []

        async def _bounded_probe(acc):
            async with sem:
                # save=False：只更新内存中的 acc.healthy，全部探完后统一写盘
                alive = await self.check_session(acc, save=False)
                if alive:
                    probe_counter["alive"] += 1
                else:
                    probe_counter["dead"] += 1
                    dead_accounts.append(acc)
                # 错峰：每个探活请求之间留一点间隔，降低短时间内集中请求的风控风险
                await asyncio.sleep(1.5)

        await asyncio.gather(*[_bounded_probe(a) for a in candidates])

        # 探活结果统一写盘一次，而非每个账号各写一次
        await self.async_save()

        log.info(
            f"⏰ 活跃池探活完成：存活 {probe_counter['alive']}，"
            f"需刷新 {probe_counter['dead']}，共 {len(candidates)} 个"
        )

        evict_targets: list[AccountState] = []
        if dead_accounts:
            limited_dead = dead_accounts[:max(0, AUTO_OTP_REFRESH_MAX_PER_TICK)] if AUTO_OTP_REFRESH else dead_accounts
            evict_targets.extend(dead_accounts[len(limited_dead):])
            log.info(f"⏰ 开始对 {len(limited_dead)} 个失效账号执行 refresh...")
            refresh_counter = {"ok": 0, "fail": 0}

            async def _bounded_refresh(acc):
                async with sem:
                    ok = await self.refresh_account(acc)
                    if ok:
                        refresh_counter["ok"] += 1
                        acc.fail_count = 0
                    else:
                        refresh_counter["fail"] += 1
                        evict_targets.append(acc)

            await asyncio.gather(*[_bounded_refresh(a) for a in limited_dead])
            log.info(
                f"⏰ refresh 完成：成功 {refresh_counter['ok']}，"
                f"失败 {refresh_counter['fail']}"
            )
        else:
            log.info("⏰ 活跃池内账号均存活，无需 refresh。")

        # refresh 仍失败的账号请出活跃池，触发补充
        for acc in evict_targets:
            await self._evict_from_pool(acc, reason="keepalive_refresh_failed")

        # 每次定期保活任务结束前，自动检查整体总池子是否满足 2倍 目标
        await self._ensure_spare_accounts()

        log.info("⏰ keepalive_tick 完成")

    # ── 轮询获取可用账号 ──────────────────────────────────
    async def get_next(self) -> AccountState:
        with self._sync_lock:
            pool_set = set(self.active_pool)
            valid = [
                a for a in self.accounts
                if a.index in pool_set and self._account_is_eligible(a)
            ]

        if not valid:
            log.warning("⚠️ 活跃池内无可用账号，尝试同步补充...")
            await self._replenish_active_pool()
            with self._sync_lock:
                pool_set = set(self.active_pool)
                valid = [
                    a for a in self.accounts
                    if a.index in pool_set and self._account_is_eligible(a)
                ]
            if not valid:
                raise HTTPException(500, "无可用账号且活跃池补充失败")
            return valid[0]

        # 只在轮询取号这一瞬间持锁，不阻塞保活
        async with self._idx_lock:
            acc = valid[self._idx % len(valid)]
            self._idx += 1

        # 活跃池数量低于阈值时，异步触发补充（不阻塞当前请求）
        if len(valid) < ACTIVE_POOL_SIZE:
            asyncio.create_task(self._replenish_active_pool())

        return acc

    # ── 换号重试逻辑 ──────────────────────────────────────
    async def mark_failed_and_get_replacement(
            self,
            acc: AccountState,
            reason: str = "unknown",
            attempt: int = 1,
    ) -> AccountState:
        """
        账号调用失败时调用。
        - 累计该账号的连续失败次数（fail_count）；
        - 未达 FAIL_THRESHOLD（默认 3）：仍可能只是抖动，先从活跃池找备用号顶上，
          但不立即把该账号请出活跃池（留待下次探活/请求验证是否恢复）；
        - 达到 FAIL_THRESHOLD：判定该账号确实故障，请出活跃池并异步触发补充
          （优先从备用池选健康账号，不够再注册新账号）；同时尝试一次保活作为最后挽救，
          失败则彻底放弃该账号，改用活跃池中的其他账号或新账号。
        """
        if reason == "quota_exhausted":
            acc.mark_quota_exhausted()
            self.save()
            asyncio.create_task(self._evict_from_pool(acc, reason))
            await self._replenish_active_pool()
            with self._sync_lock:
                pool_set = set(self.active_pool)
                valid = [
                    candidate
                    for candidate in self.accounts
                    if candidate.index in pool_set
                       and self._account_is_eligible(candidate)
                ]
            if valid:
                return valid[0]
            raise HTTPException(500, "额度耗尽账号已删除，但没有可用的替代账号")

        async with self._async_lock:
            acc.fail_count = getattr(acc, "fail_count", 0) + 1
            log.warning(
                f"❌ 账号 [{acc.email}] 请求失败，原因: {reason} "
                f"(本次重试第{attempt}次，累计连续失败 {acc.fail_count}/{FAIL_THRESHOLD})"
            )

            reached_threshold = acc.fail_count >= FAIL_THRESHOLD
            if reached_threshold:
                acc.healthy = False
                acc.disabled = True
            self.save()

            # 先尝试从活跃池里找一个可用的备用账号顶上，避免等待保活/注册
            with self._sync_lock:
                pool_set = set(self.active_pool)
                valid = [
                    a for a in self.accounts
                    if a.index in pool_set
                       and a.index != acc.index
                       and self._account_is_eligible(a)
                ]
            if valid:
                log.info(f"🔄 切换到活跃池内备用账号: {valid[0].email}")
                if reached_threshold:
                    asyncio.create_task(self._evict_from_pool(acc, reason=reason))
                return valid[0]

            # 活跃池暂无其他备用号，先尝试保活当前账号
            log.warning(f"⚠️ 活跃池暂无其他备用号，尝试保活 [{acc.email}]...")
            acc.disabled = False
            ok = await self.refresh_account(acc)
            if ok:
                acc.fail_count = 0
                acc.healthy = True
                acc.disabled = False
                self.save()
                log.info(f"✅ 保活成功，继续使用 [{acc.email}]")
                return acc
            # 保活失败则视同达到阈值，请出活跃池
            acc.healthy = False
            acc.disabled = True
            self.save()

        # 走到这里：账号已确认故障（达到阈值或保活失败），请出活跃池并补充
        await self._evict_from_pool(acc, reason=reason)

        # attempt >= 3：先尝试对该账号保活，保活失败再依赖补充逻辑给出新账号
        if attempt >= 3 and AUTO_OTP_REFRESH:
            log.warning(f"⚠️ 第 {attempt} 次重试，最后尝试保活 [{acc.email}]...")
            acc.disabled = False  # 解禁才能保活
            ok = await self.refresh_account(acc)
            if ok:
                acc.fail_count = 0
                log.info(f"✅ 保活成功，继续使用 [{acc.email}]")
                async with self._pool_lock:
                    if acc.index not in self.active_pool:
                        self.active_pool.append(acc.index)
                        await self.async_save()
                return acc
            log.warning("保活失败，改为从补充结果中取号...")

        # 等待补充任务把活跃池填满，再从中取一个可用账号返回
        await self._replenish_active_pool()
        with self._sync_lock:
            pool_set = set(self.active_pool)
            valid = [
                a for a in self.accounts
                if a.index in pool_set and self._account_is_eligible(a)
            ]
        if valid:
            return valid[0]

        # 兜底：直接注册一个新账号
        if AUTO_REGISTER_ON_REPLENISH:
            new_acc = await self._register_new_account()
            if new_acc:
                async with self._pool_lock:
                    if new_acc.index not in self.active_pool:
                        self.active_pool.append(new_acc.index)
                        await self.async_save()
                return new_acc

        raise HTTPException(500, "换号重试失败：无备用账号且自动注册/保活均未成功")

    # ── 用量记账 ──────────────────────────────────────────
    def record_usage(self, acc: AccountState) -> None:
        """Record local statistics only; quota is determined exclusively by /usage."""
        if hasattr(acc, "increment_usage"):
            acc.increment_usage()
        else:
            acc.usage_count += 1

        # 一次成功的请求记账，说明账号当前可用，清零连续失败计数
        acc.fail_count = 0

        """quota is never inferred from local request count"""
        '''
        if False:
            try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._replenish_active_pool())
                    # 额度耗尽淘汰旧号时，触发预注册以补充被销毁的空缺
                    loop.create_task(self._ensure_spare_accounts())
                except RuntimeError:
                    pass
            return

        '''
        self.save()
        log.info(f"📈 [{acc.email}] 已用: {acc.usage_count}/{ACCOUNT_MAX_USAGE}")

    # ── 账号 session 检查（usage 余额探测） ─────────────
    async def check_session(self, acc: AccountState, save: bool = True) -> bool:
        """
        通过发起一次轻量 LLM 对话来判断 session 是否仍然有效。
        - 正常返回非空文本 → 存活，返回 True
        - 返回空响应（EmptyResponseError）或 401 → 已过期，返回 False
        - 其他网络异常 → 保守视为存活（True），避免误杀
        save=False 时跳过持久化，供 keepalive_tick 批量探活后统一保存。
        """
        from app.cosverse_client import stream_cosverse_response, EmptyResponseError
        from app.routers.model_list import resolve_model
        try:
            alive, limits, error = await fetch_usage(acc.cookie_str)
            if alive:
                acc.healthy = True
                acc.message_limit = int(limits.get("messageLimit", acc.message_limit) or 0)
                acc.message_usage = int(limits.get("messageUsage", acc.message_usage) or 0)
                acc.usage_count = acc.message_usage
                if acc.message_usage < acc.message_limit:
                    acc.mark_quota_recovered()
                else:
                    acc.mark_quota_exhausted()
            else:
                acc.healthy = False
            if save:
                self.save()
            return alive
            # 保留旧代码路径的默认值，但实际探活仅调用 usage 接口。
            chunks = []
            async for chunk in stream_cosverse_response(
                    text=probe_text,
                    acc=acc,
                    model=model_id,
                    provider=provider,
                    generate_title=False,
            ):
                chunks.append(chunk)
                # 拿到第一个非空 chunk 即可确认存活，提前退出
                break

            alive = len(chunks) > 0
            acc.healthy = alive
            if save:
                self.save()
            if alive:
                log.info(f"[{acc.email}] LLM 探活：存活 ✅")
            else:
                log.warning(f"[{acc.email}] LLM 探活：空响应，视为过期 ❌")
            return alive

        except EmptyResponseError:
            log.warning(f"[{acc.email}] LLM 探活：EmptyResponseError，视为过期 ❌")
            acc.healthy = False
            if save:
                self.save()
            return False

        except PermissionError:
            log.warning(f"[{acc.email}] LLM 探活：401 cookie 过期 ❌")
            acc.healthy = False
            if save:
                self.save()
            return False

        except Exception as e:
            # 429、模型错误和网络抖动都不能证明 token 失效，避免误触发 OTP。
            log.warning(f"[{acc.email}] LLM 探活：异常（{e}），保守视为存活")
            return True


# 全局单例
cm = CookieManager()
