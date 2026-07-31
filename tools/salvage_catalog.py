import json, glob, os, sys

# One-off recovery tool: rebuilds docs/log_catalog_master.json by scraping log
# formats out of agent transcripts. Paths come from the environment or argv,
# because the originals were absolute paths into one developer's home directory
# and one specific Claude session — this could not run anywhere else.
#
#   AGENTVISION_TRANSCRIPT_DIRS=/path/a:/path/b  python3 tools/salvage_catalog.py
#
# With nothing set it scans nothing and simply reports that, rather than
# pretending to have salvaged an empty catalog.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_roots = [d for d in os.environ.get("AGENTVISION_TRANSCRIPT_DIRS", "").split(os.pathsep) if d]
_roots += sys.argv[1:]

FILES = []
for root in _roots:
    FILES += glob.glob(os.path.join(root, "*", "*.jsonl"))
    FILES += glob.glob(os.path.join(root, "*.output"))
MASTER = os.path.join(REPO, "docs", "log_catalog_master.json")
if not FILES:
    print("no transcripts to scan — set AGENTVISION_TRANSCRIPT_DIRS or pass dirs "
          "as arguments. Nothing was written.")
    raise SystemExit(0)

FMT_KEYS = ("detect_signature", "sample_line", "structure", "product")
def is_fmt(o):
    return isinstance(o, dict) and o.get("name") and any(k in o for k in FMT_KEYS)

def scan(text):
    out=[]; n=len(text); i=0
    while True:
        i=text.find("{", i)
        if i<0: break
        if text.find('"name"', i, i+300)<0: i+=1; continue
        depth=0; j=i; instr=False; esc=False; ok=False; cap=i+8000
        while j<n and j<cap:
            c=text[j]
            if esc: esc=False
            elif c=='\\': esc=True
            elif c=='"': instr=not instr
            elif not instr:
                if c=='{': depth+=1
                elif c=='}':
                    depth-=1
                    if depth==0:
                        try:
                            o=json.loads(text[i:j+1])
                            if is_fmt(o): out.append(o)
                            ok=True
                        except Exception: pass
                        break
            j+=1
        i=(j+1) if ok else (i+1)
    return out

raw=[]; scanned=0
for fp in FILES:
    try: txt=open(fp, encoding="utf-8", errors="replace").read()
    except Exception: continue
    scanned+=1; raw += scan(txt)

prior=[]
try: prior=json.load(open(MASTER))["catalog"]
except Exception: pass

seen={}
for f in prior + raw:
    k=(f.get("name") or "").strip().lower()
    if not k: continue
    if k not in seen or len(json.dumps(f))>len(json.dumps(seen[k])): seen[k]=f
cat=list(seen.values())
def pri(p): return sum(1 for f in cat if f.get("priority")==p)
json.dump({"total_unique":len(cat),"high":pri("high"),"medium":pri("medium"),"low":pri("low"),
           "catalog":sorted(cat,key=lambda f:(f.get("priority","z"),f.get("name","")))},
          open(MASTER,"w"), indent=1)
prior_n=len({(f.get('name') or '').strip().lower() for f in prior if f.get('name')})
print(f"files scanned: {scanned}")
print(f"format objects recovered: {len(raw)}")
print(f"prior master: {prior_n}  ->  NEW master: {len(cat)}  (+{len(cat)-prior_n})   high={pri('high')} med={pri('medium')} low={pri('low')}")
