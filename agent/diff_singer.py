"""
DiffSinger 歌声合成引擎封装 🎤 (v2 — MiniEngine REST API)

通过 DiffSingerMiniEngine 的 REST API 调用 ONNX 模型生成歌声。
首次调用时自动启动 MiniEngine，无需手动操作。

API 流程:
  /submit → 获取 token → /query 轮询 → /download 下载 WAV
"""

import asyncio
import json
import logging
import subprocess as _sp
import time
from pathlib import Path

import httpx

from .async_io import run_bounded_blocking

logger = logging.getLogger("糖糖.DiffSinger")

_engine: "DiffSingerEngine | None" = None

# 音高映射: MIDI 音符 → 频率 (Hz)
def midi_to_freq(midi: int) -> float:
    return 440.0 * (2 ** ((midi - 69) / 12))


class DiffSingerEngine:
    """DiffSinger 歌声合成引擎（通过 MiniEngine REST API）"""

    def __init__(self, base_url: str = "http://127.0.0.1:9266",
                 model: str = "lianhua", speedup: int = 10,
                 f0_timestep: float = 0.01):
        self.base_url = base_url
        self.model = model
        self.speedup = speedup
        self.f0_timestep = f0_timestep
        self._available: bool | None = None
        self._client: httpx.Client | None = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=httpx.Timeout(300))
        return self._client

    def is_available(self) -> bool:
        """检查 MiniEngine 是否在运行（不会自动启动）。"""
        if self._available is not None:
            return self._available
        try:
            r = self.client.get(f"{self.base_url}/models")
            if r.status_code == 200:
                models = r.json().get("models", [])
                self._available = self.model in models
                if self._available:
                    logger.info(f"✅ DiffSinger MiniEngine: model={self.model}")
                return self._available
        except Exception:
            pass
        self._available = False
        return False

    def _ensure_server(self) -> bool:
        """确保 MiniEngine 正在运行，没有就自动启动。"""
        if self.is_available():
            return True

        server_dir = Path("DiffSingerMiniEngine")
        if not server_dir.exists():
            logger.warning("DiffSingerMiniEngine 目录不存在")
            return False

        logger.info("🚀 自动启动 DiffSingerMiniEngine...")
        try:
            _sp.Popen(
                ["python", "server.py", "--config", "lianhua"],
                cwd=str(server_dir),
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            )
            # 等它启动（最多 30 秒）
            for _ in range(30):
                time.sleep(1)
                self._available = None  # 重置缓存，重新检查
                if self.is_available():
                    logger.info("✅ MiniEngine 已就绪")
                    return True
            logger.warning("MiniEngine 启动超时")
            return False
        except Exception as e:
            logger.warning(f"MiniEngine 启动失败: {e}")
            return False

    def synthesize(self, phonemes: list[dict], output_path: str,
                   f0_values: list[float] | None = None,
                   note_durations: list[float] | None = None) -> Path | None:
        """
        合成歌声 WAV。

        Args:
            phonemes: [{"name": "sh", "duration": 0.3}, ...]
            output_path: 输出文件路径
            f0_values: 基频曲线（Hz），不提供则从 phoneme MIDI 信息推断
            note_durations: 每个音符的时长（秒），配合 f0 使用
        """
        if not self._ensure_server():
            logger.warning("DiffSinger MiniEngine 未就绪")
            return None

        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        # 构建 f0 曲线
        if f0_values is None:
            # 从 phoneme 信息推断（如果有 midi 字段）
            f0_values = self._build_f0(phonemes, note_durations)

        request = {
            "model": self.model,
            "phonemes": [{"name": p["name"], "duration": p["duration"]} for p in phonemes],
            "f0": {
                "timestep": self.f0_timestep,
                "values": f0_values,
            },
            "speedup": self.speedup,
        }

        logger.info(f"🎤 提交合成: {len(phonemes)} 个音素, {len(f0_values)} 个 f0 采样点")

        # 提交任务
        try:
            r = self.client.post(f"{self.base_url}/submit", json=request)
            if r.status_code != 200:
                logger.warning(f"MiniEngine submit 失败: {r.status_code}")
                return None
            result = r.json()
        except Exception as e:
            logger.warning(f"MiniEngine 连接失败: {e}")
            return None

        token = result["token"]
        status = result["status"]

        # 如果已缓存，直接下载
        if status == "HIT_CACHE":
            logger.info(f"🎤 命中缓存: {token}")
            return self._download(token, output)

        # 轮询等待完成
        logger.debug(f"🎤 任务已提交: {token}")
        for _ in range(600):  # 最多等 10 分钟
            time.sleep(1)
            try:
                r = self.client.post(f"{self.base_url}/query", json={"token": token})
                q = r.json()
                s = q.get("status", "")
                if s == "FINISHED" or s == "HIT_CACHE":
                    return self._download(token, output)
                elif s == "FAILED":
                    logger.warning(f"MiniEngine 合成失败: {q.get('message', '未知错误')}")
                    return None
            except Exception:
                pass

        logger.warning(f"MiniEngine 合成超时: {token}")
        return None

    def _build_f0(self, phonemes: list[dict], note_durations: list[float] | None) -> list[float]:
        """从 phoneme 的 midi 字段 + 时长 构建 f0 采样序列。"""
        values = []
        for p in phonemes:
            midi = p.get("midi", 60)
            freq = midi_to_freq(midi)
            n_samples = max(1, int(p["duration"] / self.f0_timestep))
            values.extend([round(freq, 2)] * n_samples)
        return values

    def _download(self, token: str, output: Path) -> Path | None:
        """下载合成的 WAV 文件。"""
        try:
            r = self.client.get(f"{self.base_url}/download", params={"token": token})
            if r.status_code == 200:
                output.write_bytes(r.content)
                size_kb = len(r.content) / 1024
                logger.info(f"✅ {output.name} ({size_kb:.0f} KB)")
                return output
        except Exception as e:
            logger.warning(f"下载失败: {e}")
        return None

    def text_to_phonemes(self, text: str, midi_notes: list[int] | None = None,
                         durations: list[float] | None = None) -> list[dict]:
        """
        中文歌词 → 音素序列（每个音素带音高信息）。

        流程: 汉字 → pypinyin → 词典查找 → 音素列表
        """
        from pypinyin import pinyin, Style

        dict_path = Path("DiffSingerMiniEngine/assets/dictionaries/lianhua-zh.txt")
        py_to_ph: dict[str, list[str]] = {}
        if dict_path.exists():
            for line in dict_path.read_text(encoding="utf-8").strip().split("\n"):
                parts = line.strip().split("\t")
                if len(parts) == 2:
                    py_to_ph[parts[0]] = parts[1].split()

        chars = list(text.strip())
        n = len(chars)
        midi = midi_notes or ([60] * n)
        dur = durations or ([0.3] * n)

        py_list = pinyin(chars, style=Style.TONE3, errors="ignore")

        result = []
        for i, (ch, (py,)) in enumerate(zip(chars, py_list)):
            if not py:
                py = ch
            ph_list = py_to_ph.get(py, py_to_ph.get(py.rstrip("0123456789"), [py]))
            m = midi[min(i, len(midi) - 1)]
            d = dur[min(i, len(dur) - 1)]
            per_ph_dur = d / max(len(ph_list), 1)
            for ph in ph_list:
                # 莲华模型需要 zh/ 前缀
                if not ph.startswith("zh/") and ph not in ("SP", "AP"):
                    ph = f"zh/{ph}"
                result.append({
                    "name": ph,
                    "duration": round(per_ph_dur, 3),
                    "midi": m,
                })

        return result


    # ═══════════════════════════════════════
    # 异步包装（供 async 上下文调用，不阻塞事件循环）
    # ═══════════════════════════════════════

    async def ensure_server_async(self) -> bool:
        """异步版 _ensure_server：在后台线程启停 MiniEngine"""
        return await run_bounded_blocking(
            "diff_singer.ensure_server",
            self._ensure_server,
            logger=logger,
            log_prefix="🎵 DiffSinger 服务准备较慢",
        )

    async def synthesize_async(self, phonemes: list[dict], output_path: str,
                                f0_values: list[float] | None = None,
                                note_durations: list[float] | None = None) -> Path | None:
        """异步版 synthesize：在后台线程合成歌声"""
        return await run_bounded_blocking(
            "diff_singer.synthesize",
            self.synthesize,
            phonemes,
            output_path,
            f0_values,
            note_durations,
            logger=logger,
            log_prefix="🎵 DiffSinger 合成较慢",
        )


def get_diff_singer_engine(base_url: str = "http://127.0.0.1:9266",
                           model: str = "lianhua") -> DiffSingerEngine:
    """获取 DiffSingerEngine 单例。"""
    global _engine
    if _engine is None:
        _engine = DiffSingerEngine(base_url=base_url, model=model)
    return _engine
