DEPTH = 30
log = []
trace = [False]

def chain(prefix, n):
    cur = type(prefix + '0', (), {})
    for i in range(1, n):
        cur = type('%s%d' % (prefix, i), (cur,), {})
    return cur

X = chain('X', DEPTH); Y = chain('Y', DEPTH); Z = chain('Z', DEPTH)

class Evil:
    def __hash__(self):
        return hash('mro')
    def __eq__(self, other):
        if trace[0]:
            mro = type.__dict__['__mro__'].__get__(T)
            log.append((len(log), id(mro), mro[1].__name__))
        return False

Meta = type('Meta', (type,), {Evil(): 1})
T = Meta('T', (X,), {})
# Burn Meta's version budget: each setattr invalidates, each getattr on an
# instance of Meta (i.e. on T) assigns a fresh version.
for i in range(1200):
    setattr(Meta, 'v%d' % i, i)
    getattr(T, 'v%d' % i, None)
trace[0] = True
T.__bases__ = (Z,)
trace[0] = False
print("hits:", log)
print("final mro[1]:", type.__dict__['__mro__'].__get__(T)[1].__name__)
