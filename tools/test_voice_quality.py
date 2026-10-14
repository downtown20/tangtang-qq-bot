"""GPT-SoVITS 系统化测试——找最优文本格式规律"""
import requests, os, json, time
from pathlib import Path

import soundfile as sf

# 2026-08-15：相对项目根，不再硬编码盘符
_BASE = Path(__file__).resolve().parent.parent
API = "http://127.0.0.1:9880/tts"
REF = str(_BASE / "gpt-sovits" / "speakers" / "happy" / "ref.wav")
OUT = str(_BASE / "voice_cache" / "test_suite")
os.makedirs(OUT, exist_ok=True)

# ═══ 测试维度 ═══

# 维度1: 文本长度
texts = {
    "短句10字": "哥哥找我干嘛呀喵。",
    "短句15字": "哥哥找我干嘛呀是不是想我啦。",
    "中句25字": "哼，每次都是糖糖你好呀，就不能换个新鲜的开场白嘛。",
    "中句35字": "不过听到你的声音还是挺开心的，晚上好呀，今天想聊什么呢。",
    "长句50字": "哼每次都是糖糖你好呀就不能换个新鲜的开场白嘛。不过听到你的声音还是挺开心的晚上好呀。",
    "两句25字": "哥哥找我干嘛呀。是不是想我啦。",
    "两句40字": "哼每次都是糖糖你好呀就不能换个新鲜的开场白嘛。不过听到你的声音还是挺开心的。",
    "三句60字": "哥哥找我干嘛呀。是不是想我啦。我在呢一直都在。",
}

# 维度2: 标点风格
puncts = {
    "句号": "。",
    "感叹号": "！",
    "波浪号": "～",
    "混合": "。！～",
}

# 维度3: 情绪标签
emotions = {
    "无标签": "",
    "开心": "[开心]",
    "傲娇": "[傲娇]",
    "撒娇": "[撒娇]",
    "多情绪": "[傲娇]哼。不过[开心]听到声音还是挺开心的。",
}

results = []
total = len(texts) * 1  # Focus on text patterns, not punct/emotion combos

print("=" * 60)
print("  GPT-SoVITS 语音质量系统化测试")
print("=" * 60)

for ti, (tname, text) in enumerate(texts.items()):
    # Test with best known config (cut1, no streaming)
    for ei, (ename, etag) in enumerate(emotions.items()):
        if etag and etag not in ("[开心]", "[傲娇]", "[撒娇]"):
            continue  # Skip complex multi-emotion for baseline
        full_text = etag + text if etag else text

        name = f"{tname}_{ename}"
        try:
            r = requests.post(API, json={
                "text": full_text, "text_lang": "zh",
                "ref_audio_path": REF,
                "prompt_lang": "zh", "prompt_text": "你好",
                "text_split_method": "cut1", "batch_size": 1,
                "media_type": "wav", "streaming_mode": False,
            }, timeout=30)

            fname = os.path.join(OUT, f"{name}.wav")
            with open(fname, "wb") as f:
                f.write(r.content)
            info = sf.info(fname)
            dur = info.duration
            size = os.path.getsize(fname)

            # Calculate expected vs actual duration
            # Chinese ~4 chars/sec, so expected duration = len(text)/4
            expected = len(text) / 4
            coverage = dur / expected if expected > 0 else 0

            status = "✅" if coverage > 0.6 else "⚠️" if coverage > 0.3 else "❌"
            print(f"{status} {name}: {dur:.1f}s (预估{expected:.1f}s, 覆盖率{coverage:.0%})")

            results.append({
                "name": name, "text": text, "emotion": ename,
                "duration": dur, "size": size, "coverage": coverage,
                "text_len": len(text), "sentences": text.count("。"),
                "has_period": "。" in text, "has_exclaim": "！" in text,
            })
        except Exception as e:
            print(f"❌ {name}: {e}")

# ═══ 分析 ═══
print("\n" + "=" * 60)
print("  分析结论")
print("=" * 60)

good = [r for r in results if r["coverage"] > 0.6]
bad = [r for r in results if r["coverage"] <= 0.3]

if good:
    avg_len = sum(r["text_len"] for r in good) / len(good)
    print(f"\n✅ 好的文本 ({len(good)}条):")
    print(f"   平均长度: {avg_len:.0f}字")
    print(f"   句号使用率: {sum(1 for r in good if r['has_period'])/len(good):.0%}")
    print(f"   平均覆盖率: {sum(r['coverage'] for r in good)/len(good):.0%}")
    print(f"   示例: {good[0]['text'][:60]}")

if bad:
    avg_len_bad = sum(r["text_len"] for r in bad) / len(bad)
    print(f"\n❌ 差的文本 ({len(bad)}条):")
    print(f"   平均长度: {avg_len_bad:.0f}字")
    print(f"   句号使用率: {sum(1 for r in bad if r['has_period'])/len(bad):.0%}")
    print(f"   示例: {bad[0]['text'][:60]}")

# ═══ 最佳组合推荐 ═══
print("\n📋 推荐配置:")
best = sorted(results, key=lambda r: r["coverage"], reverse=True)[:5]
for r in best:
    print(f"  {r['coverage']:.0%} | {r['text_len']}字 | {r['sentences']}句 | {r['emotion']} | {r['text'][:50]}")

# Save results
with open(os.path.join(OUT, "results.json"), "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print(f"\n详细结果: {OUT}/results.json")
print(f"音频文件: {OUT}/")
