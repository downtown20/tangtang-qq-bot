"""Delete dead code functions from handler.py"""
from pathlib import Path

# 项目根由本文件位置算出（tools/_归档/ 的上两级）——2026-10-15 T55：原先写死盘符。
_HANDLER = Path(__file__).resolve().parents[2] / "agent" / "handler.py"
lines = open(_HANDLER, "r", encoding="utf-8").readlines()
print(f"Before: {len(lines)} lines")

# Functions to delete (1-indexed line numbers, inclusive)
to_delete = [
    (5256 - 59, 5255),  # _find_song_for_request (ends before _build_singing_system_prompt)
    (5179, 5196),        # _extract_section_hint
    (5120, 5178),        # _extract_song_query
    (5090, 5119),        # _is_song_request
    (3590, 3654),        # _llm_intent_recognition
]

# Delete from bottom up to preserve line numbers
to_delete.sort(key=lambda x: x[0], reverse=True)

for start, end in to_delete:
    print(f"Deleting lines {start}-{end}: {lines[start-1].strip()[:60]}...")
    del lines[start-1:end]

print(f"After: {len(lines)} lines")
open(_HANDLER, "w", encoding="utf-8").writelines(lines)
print("Done")
