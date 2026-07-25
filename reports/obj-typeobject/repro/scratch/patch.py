import re
p = 'repro_modified.py'
s = open(p).read()
s = s.replace("""            for s in subs:
                try:
                    s.__bases__ = (object,)
                except Exception as e:
                    print("   detach failed %r" % (e,), flush=True)""",
"""            for s in subs:
                for _ in range(3):
                    try:
                        s.__bases__ = (object,)
                        break
                    except Exception as e:
                        last = e
                else:
                    print("   detach failed %r" % (last,), flush=True)""")
open(p,'w').write(s)
