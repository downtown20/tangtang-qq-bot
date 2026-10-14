"""XP3提取器 v9 - UTF-16LE反推法，不依赖固定偏移"""
import struct, zlib, os, re

def extract(path, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    f = open(path, 'rb')
    full = f.read()

    idx_base = struct.unpack_from('<Q', full, 11)[0]
    idx = None
    for off in range(200):
        p = idx_base + off
        if p+2 < len(full) and full[p] == 0x78 and full[p+1] in (0x01,0x5e,0x9c,0xda):
            try:
                idx = zlib.decompress(full[p:])
                if len(idx) > 1000: break
            except: continue
    if not idx: return 0

    count = 0
    # 搜UTF-16LE编码的.ks / .tjs文件名
    for ext_bytes in [b'\x2e\x00\x6b\x00\x73\x00', b'\x2e\x00\x74\x00\x6a\x00\x73\x00']:
        start = 0
        while True:
            p = idx.find(ext_bytes, start)
            if p == -1: break
            end = p + len(ext_bytes)

            # 向后找到UTF-16LE文件名的开头
            fn_start = p
            while fn_start > 0:
                b = idx[fn_start-2:fn_start]
                # UTF-16LE字符: 高位字节为0 (ASCII) 或 高位非0 (CJK)
                if b[1] == 0 and b[0] >= 0x20:
                    fn_start -= 2
                elif b[0] >= 0x30 or b[1] >= 0x30:
                    fn_start -= 2
                else:
                    break

            fname = idx[fn_start:end].decode('utf-16-le', errors='replace')

            # 去掉段后缀 (.scn/.segm/.pimg等)
            clean = fname
            for sfx in ['.scn', '.segm', '.pimg', '.mtn', '.png']:
                if clean.lower().endswith(sfx):
                    clean = clean[:-len(sfx)]
                    break

            if not any(clean.lower().endswith(e) for e in ['.ks','.tjs','.txt','.csv']):
                start = p + 1; continue

            # 向前找最近的 "File" 标记 + entry结构
            # 文件名在条目中的位置: es + offset (offset不固定)
            # 用特征: 从fn_start向前搜 "File" + 4字节 entry_size + "info"
            entry_found = False
            for search_start in range(fn_start - 100, fn_start):
                if search_start < 0: continue
                if idx[search_start:search_start+4] != b'File': continue
                es = search_start + 4
                if es + 54 > len(idx): continue
                # 验证: es+8 应为 "info"
                if idx[es+8:es+12] != b'info': continue
                # 验证: fn_len 应在合理范围
                # fn_len在es+40到es+44之间变化，尝试找匹配的
                for fn_off in [40, 42, 44]:
                    if es + fn_off + 2 > len(idx): continue
                    test_len = struct.unpack_from('<H', idx, es + fn_off)[0]
                    test_start = es + fn_off + 2
                    if test_start + test_len*2 > len(idx): continue
                    test_name = idx[test_start:test_start + test_len*2].decode('utf-16-le', errors='replace')
                    test_clean = test_name
                    for sfx in ['.scn', '.segm', '.pimg', '.mtn', '.png']:
                        if test_clean.lower().endswith(sfx):
                            test_clean = test_clean[:-len(sfx)]; break
                    if test_clean == clean:
                        # Found match!
                        file_off = struct.unpack_from('<Q', idx, es + 24)[0]
                        comp_sz  = struct.unpack_from('<Q', idx, es + 32)[0]
                        if 0 < file_off + comp_sz <= len(full) and 0 < comp_sz < 50_000_000:
                            f.seek(file_off)
                            raw = f.read(comp_sz)
                            try: data = zlib.decompress(raw)
                            except: data = raw
                            safe = clean.replace('/','_').replace('\\','_').replace(':','_')
                            with open(os.path.join(out_dir, safe), 'wb') as o: o.write(data)
                            count += 1
                        entry_found = True
                        break
                if entry_found: break

            start = p + 1

    f.close()
    return count


if __name__ == '__main__':
    base = r"D:\claude code相关资源\千恋万花\千恋万花"
    out  = os.path.join(base, "_scripts")
    total = 0
    for fn in ["patch.xp3", "data1080.xp3", "patch_data1080.xp3"]:
        p = os.path.join(base, fn)
        if os.path.exists(p):
            mb = os.path.getsize(p)/1024/1024
            print(f"{fn} ({mb:.0f}MB):", end=" ", flush=True)
            n = extract(p, out)
            print(f"{n}个脚本")
            total += n
    print(f"\n总计{total}个 → {out}")
