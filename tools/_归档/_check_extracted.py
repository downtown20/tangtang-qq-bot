import os

base = r"D:\claude code相关资源\千恋万花\千恋万花\_scripts_dec"
files = sorted(os.listdir(base))

# Preview key scenes
key_files = [
    "102・芳乃－告白",
    "110・芳乃－初体験",
    "106・芳乃－キス",
    "014・合体編",
    "207・茉子－初体験",
]

for keyword in key_files:
    match = [f for f in files if keyword in f]
    if match:
        path = os.path.join(base, match[0])
        with open(path, 'rb') as f:
            data = f.read()
        try:
            text = data.decode('shift-jis', errors='replace')
        except:
            text = data.decode('utf-8', errors='replace')

        # Extract readable lines
        lines = []
        for line in text.split('\n'):
            line = line.strip()
            if not line: continue
            if line[0] in ('@', ';', '[', '\\', '*'): continue
            # Remove basic markup
            for tag in ['[r]', '[l]', '[p]', '[endlink]', '[link]', '[lr]', '[er]']:
                line = line.replace(tag, '')
            if line.strip():
                lines.append(line.strip())

        content = '\n'.join(lines)
        preview = content[:500].replace('\n', '\\n')
        print(f"\n{'='*60}")
        print(f"{match[0]} ({len(data)}B, {len(lines)} lines)")
        print(f"{'='*60}")
        if preview:
            print(preview)
        else:
            print("(no readable lines - might be binary .ks)")
    else:
        print(f"\nNOT FOUND: {keyword}")
