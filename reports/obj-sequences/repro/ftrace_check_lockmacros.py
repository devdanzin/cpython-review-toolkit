import sys, re
from pathlib import Path
sys.path.insert(0,"/home/danzin/projects/cpython-review-toolkit/plugins/cpython-review-toolkit/scripts")
import scan_ft_races as sfr
CPY=Path("/home/danzin/projects/cpython")
FILES=["Objects/listobject.c","Objects/bytesobject.c","Objects/bytearrayobject.c","Objects/bytes_methods.c",
       "Objects/clinic/listobject.c.h","Objects/clinic/bytearrayobject.c.h","Objects/clinic/bytesobject.c.h"]
for rel in FILES:
    src=(CPY/rel).read_text(errors="replace")
    m=sfr.discover_local_lock_macros(src)
    defines=[d for d in re.findall(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)\s*\(", src, re.M)]
    screaming=set(re.findall(r"\b([A-Z][A-Z0-9_]*LOCK[A-Z0-9_]*)\s*\(", sfr.strip_comments(src)))
    print(f"{rel}: acquire={sorted(m.acquire)} release={sorted(m.release)}")
    print(f"   function-like #defines: {len(defines)} -> {defines[:12]}")
    print(f"   SCREAMING_CASE *LOCK*( invocations (would force opaque suppression): {sorted(screaming)}")
