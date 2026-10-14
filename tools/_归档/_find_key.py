import os, re

base = r"D:\claude code相关资源\千恋万花\千恋万花\_scripts"

# Check Config.tjs and envinit.tjs for crypto settings
for fn in ['Config.tjs', 'envinit.tjs', 'keybinder.tjs']:
    p = os.path.join(base, fn)
    if not os.path.exists(p):
        print(f"{fn}: NOT FOUND")
        continue
    with open(p, 'rb') as f:
        data = f.read()
    text = data.decode('shift-jis', errors='replace')
    print(f"\n=== {fn} ({len(data)}B) ===")
    print(text[:500])
    print("...")

# Also check the patch.xp3 raw index for any crypto config
# (already done, but let's check the actual data at the file offsets)
# Check if the .scn files start with known patterns
dec_base = r"D:\claude code相关资源\千恋万花\千恋万花\_scripts_dec"
if os.path.exists(dec_base):
    files = os.listdir(dec_base)
    # Take 3 files and check their first bytes
    for fn in sorted(files)[:3]:
        p = os.path.join(dec_base, fn)
        with open(p, 'rb') as f:
            data = f.read(100)
        # Check if XOR pattern consistent across files
        print(f"\n{fn}: first 20 bytes = {data[:20].hex()}")
