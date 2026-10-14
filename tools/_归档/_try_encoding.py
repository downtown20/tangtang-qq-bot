import os

import os
base = r"D:\claude code相关资源\千恋万花\千恋万花\_scripts_dec"
files = os.listdir(base)
match = [f for f in files if '102' in f and '告白' in f]
if match:
    p = os.path.join(base, match[0])
else:
    # Just take first ks file
    ks_files = [f for f in files if f.endswith('.ks.scn')]
    p = os.path.join(base, ks_files[0])
print(f"Testing: {p}")
with open(p, 'rb') as f: data = f.read()
print(f"Size: {len(data)} bytes")

# Try different encodings
for enc in ['shift-jis', 'shift_jis', 'cp932', 'euc-jp', 'utf-8', 'utf-16-le']:
    try:
        text = data.decode(enc, errors='replace')
        jp_chars = sum(1 for c in text[:500] if '\u3040' <= c <= '\u30ff' or '\u4e00' <= c <= '\u9fff')
        print(f"\n{enc}: {jp_chars} JP/CJK chars in first 500 chars")
        if jp_chars > 20:
            # Show content
            lines = [l for l in text.split('\n') if l.strip()]
            for i, line in enumerate(lines[:15]):
                print(f"  L{i}: {line[:100]}")
            break
    except Exception as e:
        print(f"{enc}: {e}")

# Check for common KiriKiri markers
print("\n--- Checking for KiriKiri text markers ---")
for m in [b'@', b'[', b'\\r', b'\\n', b'\\wait', b'\\p', b'\\name']:
    count = data.count(m)
    if count > 3:
        print(f"  '{m.decode('ascii', errors='replace')}': {count} occurrences")

# Check: maybe XOR encrypted? Try to find the key by looking for @
# @ is very common in KiriKiri script (starts commands)
# If data is XOR-encrypted, find what key makes the most common byte into @ (0x40)
from collections import Counter
byte_counts = Counter(data[:10000])
print("\n--- Most common bytes in first 10KB ---")
for byte_val, count in byte_counts.most_common(10):
    ch = chr(byte_val) if 32 <= byte_val < 127 else '.'
    print(f"  0x{byte_val:02x} ({ch}): {count}")

# Check if most common byte maps to @ via XOR
most_common_byte = byte_counts.most_common(1)[0][0]
xor_key = most_common_byte ^ 0x40  # 0x40 = @
print(f"\nMost common byte 0x{most_common_byte:02x} XOR 0x40 = key 0x{xor_key:02x}")
# Test this key on first 1000 bytes
decoded = bytes(b ^ xor_key for b in data[:1000])
at_count = decoded.count(b'@')
print(f"After XOR 0x{xor_key:02x}: {at_count} @ signs in first 1000 bytes")
if at_count > 5:
    try:
        text = decoded.decode('shift-jis', errors='replace')
        print(f"Decrypted preview: {text[:300]}")
        # Full decrypt and save
        full_dec = bytes(b ^ xor_key for b in data)
        out_path = p.replace('.ks.scn', '_dec.ks')
        with open(out_path, 'wb') as f:
            f.write(full_dec)
        print(f"\nFULLY DECRYPTED: {out_path} ({len(full_dec)} bytes)")
    except:
        pass
