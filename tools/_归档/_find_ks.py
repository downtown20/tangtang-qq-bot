"""Find script files in XP3 index - try various encodings"""
import struct, zlib

path = r"D:\claude code相关资源\千恋万花\千恋万花\patch.xp3"
with open(path, 'rb') as f:
    full = f.read()

idx_offset = struct.unpack_from('<Q', full, 11)[0]
for off in range(200):
    p = idx_offset + off
    if p+2 < len(full) and full[p] == 0x78 and full[p+1] in (0x01, 0x5e, 0x9c, 0xda):
        index_data = zlib.decompress(full[p:])
        break

print(f"Index size: {len(index_data)} bytes")

# Search for .ks in UTF-16LE: 2e 00 6b 00 73 00
ks_utf16le = b'\x2e\x00\x6b\x00\x73\x00'
# Search for .tjs in UTF-16LE: 2e 00 74 00 6a 00 73 00
tjs_utf16le = b'\x2e\x00\x74\x00\x6a\x00\x73\x00'

for pattern, ext in [(ks_utf16le, '.ks'), (tjs_utf16le, '.tjs')]:
    positions = []
    start = 0
    while True:
        p = index_data.find(pattern, start)
        if p == -1: break
        positions.append(p)
        start = p + 1

    print(f"\n{ext}: {len(positions)} matches")

    for p in positions[:5]:
        # Find the filename start (look backwards for the start of UTF-16LE chars)
        fn_start = p
        while fn_start > 0 and fn_start % 2 == p % 2:
            b = index_data[fn_start-2:fn_start]
            if b[0] == 0 and 0x20 <= b[1] <= 0x7e:  # ASCII in UTF-16LE
                fn_start -= 2
            elif b[1] == 0 and 0x20 <= b[0] <= 0x7e:
                fn_start -= 2
            elif b[0] >= 0x30 or b[1] >= 0x30:  # CJK
                fn_start -= 2
            else:
                break

        # Read filename
        fn_end = p + len(pattern)
        raw_name = index_data[fn_start:fn_end]
        fname = raw_name.decode('utf-16-le', errors='replace')

        # Show surrounding bytes
        ctx_start = max(0, fn_start - 60)
        ctx_end = min(len(index_data), fn_end + 20)
        ctx = index_data[ctx_start:ctx_end]

        print(f"\n  File: {fname}")
        print(f"  Index pos: {p}, Filename starts: {fn_start}")
        print(f"  Context:")
        for j in range(0, len(ctx), 16):
            chunk = ctx[j:j+16]
            hex_str = ' '.join(f'{b:02x}' for b in chunk)
            ascii_str = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
            print(f"    {ctx_start+j:04x}: {hex_str:<48} {ascii_str}")

# Also check: are script files embedded differently?
# Some VNs put scripts in the .xp3 but name them differently
# Try to find any file with a script-like extension
for ext_pattern in [b'scn', b'SCN', b'csv', b'CSV']:
    c = index_data.count(ext_pattern)
    if c > 0:
        print(f"\n'{ext_pattern.decode()}': {c} occurrences")
