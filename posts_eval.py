import posts_sources as ps, posts_classify as clf
CHS = ["wbbillion","ozonmarketplace","marketpapa_channel","postavleno","sellermp",
       "redman","andrey_pro_business","kovpak_kitai","kultura_analitiki","dnative"]
posts=[]
for ch in CHS:
    try: posts += ps.fetch_channel(ch)[:7]
    except Exception as e: print("skip",ch,str(e)[:40])
print(f"МОДЕЛЬ: {clf.GEMINI_MODEL} | на классификацию: {len(posts)} постов", flush=True)
kept=0; kw=0
for p in posts:
    try: r=clf.classify(p.channel, p.text)
    except Exception as e: print("ERR",str(e)[:40]); continue
    if r.get("keep"):
        kept+=1
        print(f"[KEEP][{p.channel}] {r.get('headline','')}", flush=True)
        print(f"      apply: {r.get('apply','')}", flush=True)
print(f"=== ОСТАВЛЕНО {kept} из {len(posts)} ===", flush=True)
