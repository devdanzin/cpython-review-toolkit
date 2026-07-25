import re, sys, subprocess, os
root = "/home/danzin/projects/cpython"
files = subprocess.run(["grep","-rl","_PyEval_StopTheWorld","--include=*.c","Objects/","Python/","Modules/"],
                       cwd=root, capture_output=True, text=True).stdout.split()
CS = re.compile(r'Py_BEGIN_CRITICAL_SECTION|BEGIN_TYPE_LOCK|BEGIN_TYPE_DICT_LOCK|PyMutex_Lock')
for p in files:
    src = open(os.path.join(root,p), encoding='utf-8', errors='replace').read().split('\n')
    depth = 0; buf = []; fstart = 0
    for i, l in enumerate(src, 1):
        if depth == 0 and l.startswith('{'):
            fstart = i; buf = []
        if depth > 0 or l.startswith('{'):
            buf.append(l)
            depth += l.count('{') - l.count('}')
            if depth <= 0 and buf:
                body = '\n'.join(buf)
                if '_PyEval_StopTheWorld' in body and CS.search(body):
                    hdr = src[fstart-2].strip() if fstart >= 2 else '?'
                    print(f"{p}:{fstart}-{i}  {hdr[:60]:60s} prevent_release={'type_lock_prevent_release' in body}")
                buf = []; depth = 0
