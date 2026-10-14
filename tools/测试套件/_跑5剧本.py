"""临时批跑：5 个剧本真实对话（2026-08-11）——每剧本独立净化+独立报告"""
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

import 测试指挥 as zh

剧本s = [
    "体检报告出来了",
    "我妈开始催我相亲了",
    "我家狗查出来肿瘤",
    "前任深夜给我发了个语音",
    "减肥计划第三天就崩了",
]

ctx = zh.套件模块.Ctx("1092609076")
print(f"=== 批跑 {len(剧本s)} 个剧本 ===", flush=True)
for i, name in enumerate(剧本s, 1):
    print(f"\n========== [{i}/{len(剧本s)}] {name} ==========", flush=True)
    t0 = time.time()
    r = zh.跑真实对话(ctx, name, 轮数=6)
    print(f"\n{'-'*40}\n结果: {r}\n耗时 {int(time.time()-t0)}s\n{'='*40}", flush=True)
print("\n=== 全部完成 ===", flush=True)
