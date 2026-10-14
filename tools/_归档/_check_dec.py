import os

base = r"D:\claude code相关资源\千恋万花\千恋万花\_scripts_dec"
files = os.listdir(base)
print(f"{len(files)} files in directory")

# Find a key file to test
for keyword in ['告白', '初体験', '合体編', 'キス']:
    matches = [f for f in files if keyword in f]
    if matches:
        path = os.path.join(base, matches[0])
        print(f"\n=== {matches[0]} ===")
        with open(path, 'rb') as f:
            data = f.read()
        print(f"Size: {len(data)} bytes")
        print(f"First 40 bytes hex: {data[:40].hex()}")

        # Try decodings
        for enc in ['shift-jis', 'cp932', 'utf-8', 'utf-16-le']:
            try:
                text = data.decode(enc, errors='replace')
                jp = sum(1 for c in text[:500] if '\u3040' <= c <= '\u30ff' or '\u4e00' <= c <= '\u9fff')
                if jp > 20:
                    print(f"\n  {enc}: {jp} JP/CJK chars in first 500 - SUCCESS")
                    for line in text.split('\n')[:12]:
                        if line.strip():
                            print(f"    {line.strip()[:120]}")
                    break
                else:
                    first_chars = text[:100].replace('\x00', ' ').replace('\n', ' ')
                    print(f"  {enc}: only {jp} JP chars. Preview: {first_chars[:80]}")
            except Exception as e:
                print(f"  {enc}: {e}")
        print()
        break
else:
    print("\nNo matching files found. Available files:")
    for f in sorted(files)[:20]:
        print(f"  {f}")
