"""清理垃圾记忆 — 一次性执行"""
import sqlite3

db = sqlite3.connect('memory.db')
db.execute('PRAGMA foreign_keys = ON')
d = 0

# 1. 主人垃圾记忆
owner_ids = [12,14,1527,2773,2774,2812,2847,2869,2870,2986,3004,3114,3120,3122,3123,3124,3125,3126,3127,6367,6382,6420,6462,6476,7185]
for mid in owner_ids:
    db.execute('DELETE FROM memories WHERE id=?', (mid,))
d += len(owner_ids)
print(f'1. 主人垃圾记忆: {len(owner_ids)}条')

# 2. 重复profile_synthesis
pdupes = {2872952283:[6582,6587,6650,6661,6968,6973],3663299861:[6673,6678,6842,6866],2959482956:[7251],2806847641:[6743],2656840563:[6808],2256983956:[7014]}
c = 0
for ids in pdupes.values():
    for mid in ids:
        db.execute('DELETE FROM memories WHERE id=?', (mid,))
        c += 1
d += c
print(f'2. 重复profile_synthesis: {c}条')

# 3. 重复fact_synthesis
fdupes = {2872952283:[6583,6584,6585,6586,6588,6589,6590,6591,6592,6651,6652,6653,6654,6655,6662,6663,6664,6665,6969,6970,6971,6972,6974,6975,6976,6977,6980,6981,6982,6983],3663299861:[6674,6675,6676,6677,6679,6680,6681,6682,6683,6843,6844,6845,6846,6867,6868,6869,6870,6871,6873,6874,6875,6876],2959482956:[7252,7253,7254,7255,7256,7357,7358,7359,7360],2806847641:[6744,6745,6746,6747,6748,6750,6751,6752,6753],2656840563:[6809,6810,6811,6812,6813,6815,6816,6817,6818],2256983956:[7015,7016,7017,7018,7020,7021,7022],3086930591:[6935,6936,6937,6938],2470404655:[6713,6714,6715,6716],2377255095:[6610,6611,6612,6613],2227864564:[6824,6825,6826,6827],1304517100:[6699,6700,6701,6702],3998839347:[6598,6599,6600],3677184546:[6794,6795,6796],356283207:[7301,7302,7303],1778692112:[7381,7382,7383],1714321237:[6623,6624,6625]}
c = 0
for ids in fdupes.values():
    for mid in ids:
        db.execute('DELETE FROM memories WHERE id=?', (mid,))
        c += 1
d += c
print(f'3. 重复fact_synthesis: {c}条')

# 4. 自忆问句
c = db.execute("DELETE FROM memories WHERE origin='self' AND (value LIKE '%?%' OR value LIKE '%吗%' OR value LIKE '%呢%')").rowcount
d += c
print(f'4. 自忆问句: {c}条')

# 5. 低质碎片
c = db.execute("DELETE FROM memories WHERE importance<=2 AND recall_count=0 AND LENGTH(value)<20 AND key NOT IN ('said','event')").rowcount
d += c
print(f'5. 低质碎片: {c}条')

db.commit()

m = db.execute('SELECT COUNT(*) FROM memories').fetchone()[0]
e = db.execute('SELECT COUNT(*) FROM memory_embeddings').fetchone()[0]
o = db.execute("SELECT COUNT(*) FROM memories WHERE qq_id='2524950083'").fetchone()[0]
print(f'\n总删除: {d}条 | 剩余: {m}记忆/{e}embedding | 主人: {o}条')
