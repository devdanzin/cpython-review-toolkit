# P2-F2 setup - runs UNARMED. Builds a heap type and an instance, warms every
# lazy path (interned names, method cache, managed-dict materialization) and
# pre-builds the attribute NAMES so the payload's own allocations are the
# setattr machinery and nothing else.
class T:
    pass


class Inst:
    pass


inst = Inst()

NAMES = ["z%03d" % _k for _k in range(48)]

# Warm both setattr paths with names that are then removed, so each NAMES key
# is a genuinely new insertion when the payload runs.
for _k in range(48):
    _w = "w%03d" % _k
    setattr(T, _w, _k)
    setattr(inst, _w, _k)
for _k in range(48):
    _w = "w%03d" % _k
    delattr(T, _w)
    delattr(inst, _w)
