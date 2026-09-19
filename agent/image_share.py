"""
🖼 图片分享 — 糖糖定时从花瓣网爬二次元插画分享到群里

后台 asyncio 调度器，按配置间隔从 huaban.com 搜高质量二次元图。
下载到本地临时目录 → 生成 CQ 码 → 发到群里。

数据源：花瓣移动端 API（huaban.com/v3/search/pins），无需登录。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import json
import os
import random
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

from napcat.ws_client import SendResult, send_delivery_state
from .action_contract import (
    ActionEnvelope,
    build_action_receipt_template,
    derive_action_id,
    finalize_action_receipt_template,
)
from .action_executor import ActionExecutor
from .action_plan import ActionPlan
from .async_io import run_bounded_blocking, run_bounded_store_io

logger = logging.getLogger("糖糖.ImageShare")

# ═══════════════════════════════════════════════════════════════════
# 花瓣 API 配置
# ═══════════════════════════════════════════════════════════════════

# 搜索关键词轮换——保证每次都有新鲜感
HUABAN_QUERIES = [
    # 精选关键词，确保返回的是插画/艺术作品而非杂图
    "二次元 插画 高清",
    "二次元 少女 插画",
    "二次元 壁纸 高清",
    "anime girl illustration",
    "二次元 手绘 插画",
    "二次元 厚涂 插画",
    "二次元 唯美 插画",
    "二次元 古风 插画",
    "anime scenery illustration",
    "二次元 角色 插画",
    "二次元 同人 插画",
    "anime wallpaper artwork",
    "二次元 动漫 场景",
    "二次元 日系 插画",
]

# raw_text 中包含这些词的跳过（非插画/非艺术作品）
SKIP_TEXT_WORDS = [
    "小红书", "APP", "商品", "广告", "淘宝", "购买",
    "二维码", "微信号", "价格", "包邮", "同款", "穿搭",
    "实拍", "自拍", "照片", "截图", "聊天记录", "朋友圈",
    "抖音", "快手", "视频", "直播", "素材", "模板",
    "头像", "表情包",  # 头像和表情包太小/太随意
]


def _contains_skip_text(raw_text: str) -> bool:
    """判断花瓣描述是否命中低质量/广告词（中英文统一大小写）。"""
    text = str(raw_text or "").casefold()
    return any(str(word).casefold() in text for word in SKIP_TEXT_WORDS)


def _sha256_file(path: Path) -> str:
    """流式计算本地媒体散列，避免大图读入内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _result_message_ids(result) -> list[int]:
    ids = list(getattr(result, "chunk_ids", ()) or ())
    message_id = getattr(result, "message_id", 0)
    if message_id and message_id not in ids:
        ids.append(message_id)
    return ids


def _failed_image_asset_result() -> SendResult:
    return SendResult(False, False, error="IMAGE_ASSET_INVALID")


def _uncertain_image_share_result() -> SendResult:
    return SendResult(False, False, error="IMAGE_SEND_ERROR", uncertain=True)

# 移动端 API 请求头（绕过反爬）
HUABAN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15",
    "Accept": "application/json",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://huaban.com/",
}

# 图片下载用的浏览器头
DOWNLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://huaban.com/",
    "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
}


class ImageShareScheduler:
    """定时图片分享调度器（花瓣网数据源）"""

    def __init__(
        self,
        config: dict,
        send_group_msg,       # async (group_id, text) -> None
        get_group_ids,        # () -> list[str]
        llm_caller=None,
        get_blacklist=None,   # () -> set[str]  实时查黑名单
        enrich=None,          # (text, group_id) -> str  发送前清洗（2026-08-16 链路审计）
        receipt_store=None,   # Store：定时媒体 terminal receipt 的唯一持久化入口
    ):
        self._cfg = config
        self._send = send_group_msg
        self._get_groups = get_group_ids
        self._get_blacklist = get_blacklist
        self._llm = llm_caller
        self._enrich = enrich
        self._receipt_store = receipt_store

        self._enabled = bool(config.get("enabled", True))
        self._interval_minutes = float(config.get("interval_minutes", 240))
        self._target_groups = config.get("groups", [])
        self._caption_mode = str(config.get("caption", "random"))
        self._max_per_day = int(config.get("max_per_day", 6))
        self._min_width = int(config.get("min_width", 800))
        self._min_height = int(config.get("min_height", 600))
        self._use_local_only = bool(config.get("use_local_only", False))
        self._local_dir = str(config.get("local_dir", "./share_images"))
        self._play_mode = str(config.get("play_mode", "random"))  # random / sequential
        self._play_category = str(config.get("play_category", ""))  # 空=全部，否则只播该子文件夹
        self._seq_index = 0  # 顺序播放当前位置（启动后从 _seq_state.json 恢复）
        self._pending_seq_dir: Path | None = None
        self._pending_seq_next: int | None = None

        self._sent_today: int = 0
        self._attempts_today: int = 0
        self._uncertain_today: int = 0
        self._inflight: bool = False
        self._last_date: str = ""
        self._max_attempts_per_day = int(
            config.get("max_attempts_per_day", self._max_per_day)
        )
        default_state = Path(self._local_dir or "./share_images") / "_share_state.json"
        # 工厂调用若没有配置持久化目录，保留旧的纯内存行为（尤其是
        # 临时/测试调度器）；正式 config.yaml 有 local_dir，因此状态写穿。
        state_path = config.get("state_file")
        self._state_file = (
            Path(str(state_path or default_state))
            if state_path or "local_dir" in config else None
        )
        self._task: asyncio.Task | None = None
        self._tmp_dir: Path | None = None
        self._state_corrupt = False
        self._load_runtime_state()
        self._reset_daily(datetime.now().strftime("%Y-%m-%d"))

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self):
        if not self._enabled:
            logger.info("🖼 图片分享未启用")
            return
        if self._state_corrupt:
            logger.error("🖼 图片分享状态损坏，自动分享保持停用")
            return
        if self._task and not self._task.done():
            return

        self._tmp_dir = Path(tempfile.gettempdir()) / "tangtang_share"
        self._tmp_dir.mkdir(parents=True, exist_ok=True)

        self._task = asyncio.create_task(self._loop())
        logger.info(f"🖼 图片分享已启动（花瓣网）: 每 {self._interval_minutes} 分钟")

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()

    async def _loop(self):
        """每 N 分钟发一张图"""
        # 测试模式（间隔<5分钟）快速启动，否则按配置的间隔等
        initial_wait = 30 if self._interval_minutes < 5 else self._interval_minutes * 60
        logger.info(f"🖼 图片分享将在 {initial_wait} 秒后开始，间隔 {self._interval_minutes} 分钟")
        await asyncio.sleep(initial_wait)
        while True:
            try:
                await self._share_one()
                logger.info("🫀 图片分享心跳")
                await asyncio.sleep(max(5, self._interval_minutes * 60))
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("图片分享出错")
                await asyncio.sleep(30)

    # ════════════════════════════════════════════════════════════
    # 核心：从花瓣拉图
    # ════════════════════════════════════════════════════════════

    async def _share_one(self):
        """发一张图；返回是否至少有一个目标明确确认送达。"""
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        self._reset_daily(today)

        if self._state_corrupt:
            logger.error("🖼 图片分享状态待人工修复，跳过自动发送")
            return False

        # ambiguous/inflight 代表上次 POST 可能已经实际投递。当天冻结自动分享，
        # 避免按间隔反复重发；确定失败也受独立尝试上限约束。
        if (self._inflight
                or self._sent_today >= self._max_per_day
                or self._attempts_today >= self._max_attempts_per_day
                or self._uncertain_today > 0):
            return False

        local_path = None
        caption = ""

        # 1. 优先本地图库
        local_img, local_caption = self._pick_local_image()
        if local_img:
            local_path = local_img
            caption = local_caption if local_caption else await self._generate_caption("精选美图")
        elif not self._use_local_only:
            # 2. 本地没有 → 花瓣兜底
            query = random.choice(HUABAN_QUERIES)
            pins = await self._fetch_pins(query)
            if pins:
                good = [p for p in pins
                        if p.get("file", {}).get("width", 0) >= self._min_width
                        and p.get("file", {}).get("height", 0) >= self._min_height
                        and not _contains_skip_text(p.get("raw_text", ""))]
                if good:
                    pin = random.choice(good)
                    local_path = await self._download_image(pin["file"]["url"])
                    caption = await self._generate_caption(pin.get("raw_text", "")[:60] or query)

        if not local_path:
            return False

        # 发送。每次已持久化的尝试序号是 scheduler 的稳定 source；同 source
        # 恢复时只读取 terminal receipt，不会把同一媒体再次推向网关。
        groups = self._get_target_groups()
        if not groups:
            return False
        # 外部副作用前先写穿。进程若在 await 中崩溃，重启会把 inflight
        # 转为 uncertain 并冻结当天，不会盲目重放。
        self._attempts_today += 1
        self._inflight = True
        if not self._save_runtime_state():
            self._attempts_today -= 1
            self._inflight = False
            logger.error("🖼 无法持久化图片发送尝试，已中止发送")
            return False
        source_id = f"image_share:v1:{today}:{self._attempts_today}"
        asset_payload = await self._freeze_image_asset(local_path)
        if asset_payload is None:
            self._inflight = False
            self._save_runtime_state()
            return False

        plans: list[ActionPlan] = []
        try:
            for group_id in groups:
                target = str(group_id)
                group_caption = caption
                if self._enrich and group_caption:
                    group_caption = str(self._enrich(group_caption, target) or "").strip()
                image_payload = dict(asset_payload)
                if group_caption:
                    image_payload["caption"] = group_caption
                children = [ActionEnvelope(
                    action_id=derive_action_id(
                        source_id=source_id, scope_id=target, kind="image",
                        channel="group", target=target, payload=image_payload,
                        ordinal=0, schema_version=2,
                    ),
                    kind="image", channel="group", target=target,
                    payload=image_payload, source_id=source_id, scope_id=target,
                    ordinal=0, schema_version=2,
                )]
                plans.append(ActionPlan.create(
                    source_id=source_id, scope_id=target, channel="group",
                    target=target, children=children,
                    created_at=now.isoformat(timespec="seconds"),
                    library_id=str(asset_payload["library_id"]),
                ))
        except (TypeError, ValueError):
            self._inflight = False
            self._save_runtime_state()
            logger.exception("🖼 图片分享 ActionPlan 冻结失败")
            return False

        confirmed_any = False
        uncertain_any = False
        for plan in plans:
            prior_receipts = {}
            get_receipts = getattr(self._receipt_store, "get_action_receipts", None)
            if callable(get_receipts):
                try:
                    prior_receipts = await run_bounded_store_io(
                        "image_share.get_action_receipts", get_receipts,
                        plan.scope_id, [child.action_id for child in plan.children],
                        logger=logger, log_prefix="🖼 图片分享 receipt 读取较慢",
                    ) or {}
                except Exception:
                    logger.exception("🖼 图片分享历史回执读取失败，按保守新动作处理")

            async def _dispatch(child: ActionEnvelope):
                actual = {
                    "delivery_kind": "image",
                    "asset_ref": str(child.payload["asset_ref"]),
                    "asset_sha256": str(child.payload["asset_sha256"]),
                    "library_id": str(child.payload["library_id"]),
                }
                caption = str(child.payload.get("caption") or "")
                if caption:
                    actual["text"] = caption
                image_transport = await self._frozen_image_transport(child)
                transport = (
                    f"{caption}\n{image_transport}"
                    if caption and image_transport else image_transport
                )
                template = build_action_receipt_template(child, actual)
                try:
                    result = (
                        await self._send(plan.target, transport, receipt_template=template)
                        if transport else _failed_image_asset_result()
                    )
                except Exception:
                    logger.exception(
                        "🖼 图片分享发送响应丢失: target=%s ordinal=%s",
                        plan.target, child.ordinal,
                    )
                    result = _uncertain_image_share_result()
                state = send_delivery_state(result)
                actual["partial_delivery"] = state != "confirmed"
                receipt = finalize_action_receipt_template(
                    build_action_receipt_template(child, actual), status=state,
                    message_ids=_result_message_ids(result),
                    error_code=str(getattr(result, "error", "") or ""),
                )
                await self._persist_terminal_action_receipt(receipt, result)
                return receipt

            execution = await ActionExecutor(_dispatch).execute(
                plan, prior_receipts=prior_receipts,
            )
            for receipt in execution.receipts:
                if receipt.kind == "image" and receipt.status == "confirmed":
                    confirmed_any = True
                if receipt.status == "uncertain":
                    uncertain_any = True
            logger.info(
                "🖼 图片分享 ActionPlan → 群%s (status=%s plan=%s)",
                plan.target, execution.status, plan.plan_id,
            )
            await asyncio.sleep(random.uniform(3, 8))

        # 每日成功配额只统计至少一个群明确送达的图片。
        cursor_committed = True
        if confirmed_any:
            self._sent_today += 1
            cursor_committed = self._commit_sequential_selection()
            if not cursor_committed:
                # 图片已经送达、但顺序游标没有稳定写盘。保留发送前已落盘的
                # inflight=true；重启会隔离为 uncertain，避免再发同一张图。
                uncertain_any = True
                logger.error("🖼 顺序播放游标写盘失败，冻结自动分享防止重复图片")
        if uncertain_any:
            self._uncertain_today += 1
        if cursor_committed:
            self._inflight = False
            if not self._save_runtime_state():
                # 磁盘上的发送前占位仍是 inflight=true；内存也保持冻结，
                # 不能因为终态写盘失败就在本进程再次发送。
                self._inflight = True
        self._cleanup_old_files(keep=20)
        return confirmed_any

    async def _freeze_image_asset(self, local_path: Path) -> dict | None:
        """在任何发送前冻结图片的根目录、相对路径和内容散列。"""
        try:
            asset = Path(local_path).resolve()
            if not asset.is_file():
                raise ValueError("image asset is missing")
            digest = await run_bounded_blocking(
                "image_share.asset_sha256", _sha256_file, asset,
                logger=logger, log_prefix="🖼 图片资产哈希较慢",
            )
            return {
                "asset_ref": asset.name,
                "asset_sha256": digest,
                "asset_valid": True,
                "library_id": str(asset.parent),
            }
        except (OSError, RuntimeError, ValueError):
            logger.exception("🖼 图片资产冻结失败，拒绝发送")
            return None

    async def _frozen_image_transport(self, child: ActionEnvelope) -> str:
        """仅发送与 child 中冻结散列一致的本地图片。"""
        try:
            root = Path(str(child.payload["library_id"])).resolve()
            asset = (root / str(child.payload["asset_ref"])).resolve()
            asset.relative_to(root)
            if not asset.is_file() or child.payload.get("asset_valid") is not True:
                return ""
            digest = await run_bounded_blocking(
                "image_share.verify_asset_sha256", _sha256_file, asset,
                logger=logger, log_prefix="🖼 图片资产校验较慢",
            )
            if digest != str(child.payload["asset_sha256"]):
                return ""
            return f"[CQ:image,file=file:///{asset.as_posix()}]"
        except (OSError, RuntimeError, ValueError):
            return ""

    async def _persist_terminal_action_receipt(self, receipt: dict, result) -> bool:
        """终局动作才进入 receipt mailbox；retryable 交既有 outbox 恢复。"""
        if getattr(result, "retryable", False):
            return False
        enqueue = getattr(self._receipt_store, "enqueue_action_receipt", None)
        if not callable(enqueue):
            logger.warning("🖼 图片分享无 receipt store，终局未持久化")
            return False
        try:
            inserted = await run_bounded_store_io(
                "image_share.enqueue_action_receipt", enqueue, receipt,
                logger=logger, log_prefix="🖼 图片分享 receipt 写入较慢",
            )
            logger.info(
                "🖼 图片分享动作终局已记录: kind=%s status=%s new=%s",
                receipt["kind"], receipt["status"], bool(inserted),
            )
            return True
        except Exception:
            logger.exception("🖼 图片分享动作终局写入失败")
            return False

    def _pick_local_image(self) -> tuple[Path | None, str]:
        """从本地 share_images/ 选一张图。返回 (图片路径, 配文)。
        play_mode=random: 随机选
        play_mode=sequential: 按 _order.json 中的编排顺序播放"""
        self._pending_seq_dir = None
        self._pending_seq_next = None
        local_dir = Path(self._local_dir) if self._local_dir else Path("./share_images")
        if not local_dir.exists():
            return None, ""
        exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

        if self._play_category:
            search_dir = local_dir / self._play_category
            if not search_dir.exists():
                search_dir = local_dir
        else:
            search_dir = local_dir

        files = _get_ordered_files(search_dir, exts)
        if not files:
            return None, ""

        if self._play_mode == "sequential":
            # 从文件恢复上次位置（防重启重置）
            saved = _load_seq_state(search_dir)
            self._seq_index = saved if saved is not None else self._seq_index
            self._seq_index = self._seq_index % len(files)
            chosen = files[self._seq_index]
            self._pending_seq_dir = search_dir
            self._pending_seq_next = self._seq_index + 1
        else:
            chosen = random.choice(files)

        caption = ""
        txt_path = chosen.with_suffix(".txt")
        if txt_path.exists():
            try:
                caption = txt_path.read_text(encoding="utf-8").strip()
                if caption:
                    logger.info(f"🖼 使用本地配文: {txt_path.name} → {caption[:40]}")
            except Exception:
                pass
        return chosen, caption

    async def _fetch_pins(self, query: str, limit: int = 15) -> list[dict]:
        """调花瓣移动端搜索 API"""
        try:
            async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
                r = await client.get(
                    f"https://huaban.com/v3/search/pins?q={query}&limit={limit}",
                    headers=HUABAN_HEADERS,
                )
                if r.status_code != 200:
                    logger.warning(f"🖼 花瓣API {r.status_code}")
                    return []
                data = r.json()
                pins = data.get("pins", [])
                logger.debug(f"🖼 花瓣搜索 '{query}' → {len(pins)} 张")
                return pins
        except Exception as e:
            logger.warning(f"🖼 花瓣API异常: {e}")
            return []

    async def _download_image(self, url: str) -> Path | None:
        """下载图片到本地，确定扩展名"""
        try:
            async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
                resp = await client.get(url, headers=DOWNLOAD_HEADERS)
                if resp.status_code != 200:
                    return None

                # 确定扩展名
                ct = resp.headers.get("content-type", "")
                ext = ".jpg"
                if "png" in ct:
                    ext = ".png"
                elif "webp" in ct:
                    ext = ".webp"
                elif "gif" in ct:
                    ext = ".gif"

                ts = int(time.time() * 1000)
                fname = f"huaban_{ts}_{random.randint(100, 999)}{ext}"
                fpath = self._tmp_dir / fname if self._tmp_dir else Path(tempfile.gettempdir()) / fname
                await run_bounded_blocking(
                    "image_share.download_write",
                    fpath.write_bytes,
                    resp.content,
                    logger=logger,
                    log_prefix="🖼 图片下载文件写入较慢",
                )
                logger.debug(f"🖼 下载完成: {fname} ({len(resp.content)/1024:.0f}KB)")
                return fpath

        except Exception as e:
            logger.warning(f"🖼 下载失败: {e}")
            return None

    async def _generate_caption(self, hint: str) -> str:
        """生成配文"""
        mode = self._caption_mode
        if mode == "none":
            return ""

        if mode == "llm" and self._llm:
            try:
                prompt = (
                    f"你刷到了一张二次元插画（{'标签: ' + hint if hint else '好看的图'}）。"
                    f"用1句话自然地分享给群友，像刷手机看到好图顺手转发。不要用括号动作。"
                )
                reply = await self._llm(prompt, "分享一张图")
                reply = reply.strip().strip('"').strip("'")
                if reply and len(reply) < 60:
                    return reply
            except Exception:
                pass

        # 随机配文
        captions = [
            "刷到这张，画得好好看！分享给你们~ 🎨",
            "哇这张！！忍不住存了，你们快看",
            "看到好看的图了，分享一下 ✨",
            "嘿嘿，又存了一张好图~ 喏",
            "刷到了！这个画风好喜欢 👀",
            "啊这张好好看…分享给你们",
            "猫娘雷达响了！发现好看的插画 📡",
            "糖糖刷到的，不许说不好看！",
            "存了存了，顺便发群里",
            "这个！！（尾巴尖兴奋地抖）",
            "你们看这个！画得好好好好好看",
            "二次元的魔力…分享给你们",
        ]
        return random.choice(captions)

    # ════════════════════════════════════════════════════════════
    # 工具方法
    # ════════════════════════════════════════════════════════════

    def _get_target_groups(self) -> list[str]:
        if self._target_groups:
            groups = self._target_groups
        else:
            groups = self._get_groups()
        # 实时查黑名单（支持热更新）
        blacklist = self._get_blacklist() if self._get_blacklist else set()
        return [g for g in groups if g not in blacklist]

    def _reset_daily(self, today: str):
        if self._state_corrupt:
            return
        if not self._last_date:
            self._last_date = today
            self._save_runtime_state()
        if today != self._last_date:
            self._sent_today = 0
            self._attempts_today = 0
            self._uncertain_today = 0
            self._inflight = False
            self._last_date = today
            self._save_runtime_state()

    def _load_runtime_state(self) -> None:
        """恢复每日配额；遗留 inflight 一律冻结为 uncertain。"""
        if self._state_file is None:
            return
        if not self._state_file.exists():
            return
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("share state must be an object")
            self._last_date = str(data.get("date", ""))
            self._sent_today = max(0, int(data.get("sent", 0)))
            self._attempts_today = max(0, int(data.get("attempts", 0)))
            self._uncertain_today = max(0, int(data.get("uncertain", 0)))
            self._inflight = bool(data.get("inflight", False))
            if self._inflight:
                self._uncertain_today += 1
                self._inflight = False
                self._save_runtime_state()
            self._state_corrupt = False
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._state_corrupt = True
            logger.exception("🖼 图片分享状态损坏，保留原文件并禁止自动发送")

    def _save_runtime_state(self) -> bool:
        """原子写穿每日发送状态。"""
        if self._state_file is None:
            return True
        if self._state_corrupt:
            logger.error("🖼 图片分享状态损坏，拒绝覆盖原文件")
            return False
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_file.with_suffix(self._state_file.suffix + ".tmp")
            tmp.write_text(json.dumps({
                "date": self._last_date,
                "sent": self._sent_today,
                "attempts": self._attempts_today,
                "uncertain": self._uncertain_today,
                "inflight": self._inflight,
            }, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._state_file)
            return True
        except OSError as e:
            logger.warning(f"🖼 图片分享状态持久化失败: {e}")
            return False

    def _commit_sequential_selection(self) -> bool:
        """只有至少一个目标 confirmed 后才推进顺序播放游标。"""
        if self._pending_seq_dir is None or self._pending_seq_next is None:
            return True
        if _save_seq_state(self._pending_seq_dir, self._pending_seq_next):
            self._seq_index = self._pending_seq_next
            self._pending_seq_dir = None
            self._pending_seq_next = None
            return True
        return False

    def _cleanup_old_files(self, keep: int = 20):
        if not self._tmp_dir or not self._tmp_dir.exists():
            return
        files = sorted(
            list(self._tmp_dir.glob("*.jpg")) + list(self._tmp_dir.glob("*.png")) +
            list(self._tmp_dir.glob("*.webp")) + list(self._tmp_dir.glob("*.gif")),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        for f in files[keep:]:
            try:
                f.unlink()
            except OSError:
                pass


# ═══════════════════════════════════════════════════════════════════
# 便利工厂
# ═══════════════════════════════════════════════════════════════════

def _get_ordered_files(directory: Path, exts: set) -> list[Path]:
    """按 _order.json 中的编排顺序返回图片列表。
    没有 order 文件时按文件名排序。新图自动追加到末尾。"""
    order_file = directory / "_order.json"
    ordered = []
    if order_file.exists():
        try:
            ordered = json.loads(order_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            ordered = []

    # 当前文件夹里所有图片
    current = {f.name for f in directory.iterdir() if f.suffix.lower() in exts and f.is_file()}

    # 保留 order 中仍然存在的文件
    valid_order = [name for name in ordered if name in current]
    # 新文件追加到末尾
    new_files = sorted(current - set(valid_order))
    final_order = valid_order + new_files

    # 如果顺序变了，写回
    if final_order != ordered:
        _save_order(directory, final_order)

    return [directory / name for name in final_order if (directory / name).exists()]


def _save_order(directory: Path, filenames: list[str]):
    """保存编排顺序到 _order.json"""
    try:
        order_file = directory / "_order.json"
        order_file.write_text(json.dumps(filenames, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _load_seq_state(directory: Path) -> int | None:
    """从 _seq_state.json 读取上次播放位置"""
    state_file = directory / "_seq_state.json"
    if not state_file.exists():
        return None
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        return int(data.get("index", 0))
    except (json.JSONDecodeError, OSError, ValueError):
        return None


def _save_seq_state(directory: Path, index: int) -> bool:
    """保存当前播放位置到 _seq_state.json"""
    try:
        state_file = directory / "_seq_state.json"
        state_file.write_text(json.dumps({"index": index}, ensure_ascii=False), encoding="utf-8")
        return True
    except OSError:
        return False


def create_image_share_scheduler(
    config: dict,
    send_group_msg,
    get_group_ids,
    llm_caller=None,
    get_blacklist=None,
    enrich=None,
    receipt_store=None,
) -> ImageShareScheduler:
    cfg = config.get("image_share", {})
    return ImageShareScheduler(
        config=cfg,
        send_group_msg=send_group_msg,
        get_group_ids=get_group_ids,
        llm_caller=llm_caller,
        get_blacklist=get_blacklist,
        enrich=enrich,
        receipt_store=receipt_store,
    )


# ═══════════════════════════════════════════════════════════════════
# 技能注册 —— 让 LLM 能按需搜图配文
# ═══════════════════════════════════════════════════════════════════

async def fetch_huaban_image(topic: str = "", min_width: int = 800, min_height: int = 600) -> str:
    """
    从花瓣网搜一张图并下载到本地。
    返回格式化的结果字符串，包含 CQ 码和图片信息，供 LLM 使用。
    """
    query = topic.strip() if topic and topic.strip() else random.choice(HUABAN_QUERIES)
    # 如果用户给的主题很短，加上"二次元"提高命中率
    if topic and len(query) <= 4 and "二次元" not in query:
        query = f"二次元 {query}"

    # 1. 搜图
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            r = await client.get(
                f"https://huaban.com/v3/search/pins?q={query}&limit=15",
                headers=HUABAN_HEADERS,
            )
            if r.status_code != 200:
                return f"[花瓣搜索失败: HTTP {r.status_code}]"
            pins = r.json().get("pins", [])
    except Exception as e:
        return f"[花瓣搜索异常: {e}]"

    if not pins:
        return f"[花瓣没有搜到关于'{query}'的图]"

    # 2. 过滤分辨率 + 内容质量
    good = []
    for p in pins:
        w = p.get("file", {}).get("width", 0)
        h = p.get("file", {}).get("height", 0)
        if w < min_width or h < min_height:
            continue
        raw = p.get("raw_text", "")
        if _contains_skip_text(raw):
            continue
        good.append(p)
    if not good:
        # 放宽分辨率（但不过滤内容质量）
        good = [p for p in pins
                if not _contains_skip_text(p.get("raw_text", ""))]
    if not good:
        good = pins  # 实在没有就随便来

    pin = random.choice(good)
    img_url = pin["file"]["url"]
    w = pin["file"]["width"]
    h = pin["file"]["height"]
    desc = pin.get("raw_text", "")[:80]

    # 3. 下载
    local_path = None
    try:
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
            resp = await client.get(img_url, headers=DOWNLOAD_HEADERS)
            if resp.status_code != 200:
                return f"[图片下载失败: HTTP {resp.status_code}]"

            ct = resp.headers.get("content-type", "")
            ext = ".jpg"
            if "png" in ct:
                ext = ".png"
            elif "webp" in ct:
                ext = ".webp"
            elif "gif" in ct:
                ext = ".gif"

            import tempfile
            ts = int(time.time() * 1000)
            fname = f"huaban_skill_{ts}{ext}"
            tmp_dir = Path(tempfile.gettempdir()) / "tangtang_share"
            await run_bounded_blocking(
                "image_share.skill_tmp_dir_create",
                tmp_dir.mkdir,
                parents=True,
                exist_ok=True,
                logger=logger,
                log_prefix="🖼 图片技能临时目录创建较慢",
            )
            local_path = tmp_dir / fname
            await run_bounded_blocking(
                "image_share.skill_download_write",
                local_path.write_bytes,
                resp.content,
                logger=logger,
                log_prefix="🖼 图片技能文件写入较慢",
            )
    except Exception as e:
        return f"[图片下载异常: {e}]"

    if not local_path:
        return "[图片下载失败]"

    cq = f"[CQ:image,file=file:///{local_path.resolve().as_posix()}]"
    file_size = await run_bounded_blocking(
        "image_share.skill_download_stat",
        local_path.stat,
        logger=logger,
        log_prefix="🖼 图片技能文件状态读取较慢",
    )
    size_kb = file_size.st_size / 1024

    # 返回信息给 LLM，让它自己组织语言
    return (
        f"[已获取图片]\n"
        f"CQ码（直接复制到回复中即可发送图片）: {cq}\n"
        f"图片信息: {w}x{h}, {size_kb:.0f}KB, 关键词: {query}\n"
        f"描述: {desc if desc else '无'}\n"
        f"请在回复中直接使用上面的CQ码，并配上你的文字。"
    )


def _register():
    """注册 share_image 技能"""
    from .skills import register_skill

    @register_skill(
        "share_image",
        "从花瓣网搜索并返回二次元插画/图片。群友想看美图时，给他们找真实的图——而不是靠你自己描述一张不存在的图。获取到图片后，在回复中直接使用返回的CQ码，并组织语言描述这张图。",
        {"topic": "搜索主题，如'猫娘'、'古风'、'风景'、'二次元少女'。留空则随机"},
    )
    async def _share_image_skill(topic: str = "") -> str:
        return await fetch_huaban_image(topic=topic)
