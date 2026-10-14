"""
群点赞
/点赞相册 → 最近发图的群友（chat_log）
/点赞全员 → 全群成员（get_group_member_list API，上限80人受日配额限制）
"""

import asyncio
import logging
import random
import time

from .async_io import run_bounded_store_io

logger = logging.getLogger("糖糖.Album")


class AlbumLiker:

    def __init__(self, napcat_client, handler):
        self.napcat = napcat_client
        self.handler = handler
        self._liked_users: dict[str, set] = {}

    async def daily_like_targets(self, targets: list[str], count_per_person: int = 10) -> dict:
        """每日定时点赞指定用户——每人直接给 count_per_person 次赞。
        QQ 端仍可能返回每日配额上限；该结果是已知业务终态，不是机器人后台异常。"""
        logger.info(f"💝 每日定时点赞: {len(targets)}人, 每人{count_per_person}次")
        success = fail = capped = 0
        for qq_id in targets:
            qq_id = str(qq_id).strip()
            if not qq_id:
                continue
            try:
                result = await self.napcat._call_api("send_like", {
                    "user_id": int(qq_id),
                    "times": count_per_person,
                })
                if result.get("status") == "ok":
                    success += 1
                    logger.info(f"  ✓ 每日点赞 {qq_id} x{count_per_person}")
                elif self._is_daily_quota_result(result):
                    capped += 1
                    logger.info("  ⏭ 每日点赞达到 QQ 日配额，跳过本目标")
                else:
                    fail += 1
                    logger.warning(f"  ✗ 每日点赞失败 {qq_id}: {result}")
            except Exception as e:
                fail += 1
                logger.warning(f"  ✗ 每日点赞异常 {qq_id}: {e}")
            # 每人之间等 6-12 秒，避免频率限制
            await asyncio.sleep(random.uniform(6, 12))
        logger.info(
            f"每日定时点赞完成: 成功{success}, 失败{fail}, 配额{capped}"
        )
        return {"liked": success, "failed": fail, "capped": capped}

    @staticmethod
    def _is_daily_quota_result(result: object) -> bool:
        """识别 QQ 明确返回的每日点赞配额终态。"""
        if not isinstance(result, dict):
            return False
        wording = str(result.get("wording", "") or "")
        return (
            "OIDB error 20003" in wording
            or "今日同一好友点赞数已达上限" in wording
        )

    async def scan_and_like(self, group_id: str, mode: str = "safe") -> dict:
        """从 chat_log 找最近发图的群友点赞"""
        try:
            runner = getattr(self.handler, "_run_store_io", None)
            if callable(runner):
                rows = await runner(
                    "album.scan.find_recent_image_senders",
                    self.handler.memory.store.find_recent_image_senders,
                    group_id, self.handler.bot_qq,
                )
            else:
                rows = await run_bounded_store_io(
                    "album.scan.find_recent_image_senders",
                    self.handler.memory.store.find_recent_image_senders,
                    group_id, self.handler.bot_qq,
                    logger=logger,
                    log_prefix="💝 相册 Store SQLite 调用较慢",
                )
        except Exception as e:
            logger.error(f"查询聊天记录失败: {e}")
            return {"liked": 0, "skipped": 0, "errors": 1}

        if not rows:
            return {"liked": 0, "skipped": 0, "errors": 0, "album_count": 0}

        return await self._like_list(group_id, list(rows), mode)

    async def like_all_members(self, group_id: str, mode: str = "safe") -> dict:
        """获取全群成员列表，点赞"""
        result = await self.napcat._call_api("get_group_member_list", {"group_id": int(group_id)})
        if result.get("status") != "ok":
            return {"liked": 0, "skipped": 0, "errors": 1, "error": "获取成员列表失败"}

        members = result.get("data", [])
        names = [(str(m["user_id"]), m.get("nickname", m.get("card", str(m["user_id"]))))
                 for m in members
                 if str(m.get("user_id", "")) != self.handler.bot_qq]
        return await self._like_list(group_id, names, mode)

    async def _like_list(self, group_id: str, targets: list, mode: str) -> dict:
        if group_id not in self._liked_users:
            self._liked_users[group_id] = set()
        seen = self._liked_users[group_id]

        liked = 0
        skipped = 0
        errors = 0
        total = len(targets)

        for qq_id, nickname in targets:
            if qq_id in seen:
                skipped += 1
                continue
            t = self.handler._like_tracker.get(qq_id, {})
            today = time.strftime("%Y-%m-%d")
            if t.get("date", "") != today:
                t["count"] = 0
                t["date"] = today
            if t.get("count", 0) >= self.handler.LIKE_DAILY_CAP_PER_USER:
                skipped += 1
                continue
            total_today = sum(
                v.get("count", 0) for k, v in self.handler._like_tracker.items()
                if v.get("date", "") == today
            )
            if total_today >= self.handler.LIKE_DAILY_CAP_TOTAL:
                skipped += 1
                continue

            seen.add(qq_id)
            try:
                api_result = await self.napcat._call_api("send_like", {
                    "user_id": int(qq_id),
                    "times": 1,
                })
                if api_result.get("status") == "ok":
                    self.handler._record_like(qq_id)
                    liked += 1
                    logger.info(f"  ✓ 点赞 {nickname}({qq_id})")
                else:
                    errors += 1
            except Exception as e:
                errors += 1
                logger.warning(f"  ✗ 点赞失败 {nickname}: {e}")

            delay = (6 + hash(qq_id) % 7) if mode == "safe" else (3 + hash(qq_id) % 4)
            await asyncio.sleep(delay)

        logger.info(f"群{group_id}点赞完成: liked={liked} skipped={skipped} errors={errors} total={total}")
        return {"liked": liked, "skipped": skipped, "errors": errors, "album_count": total}

    _last_scan: dict[str, float] = {}
