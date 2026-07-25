import atexit, sys, threading

# 1. super() with zero args from C, during shutdown, with no Python frame left.
def at_shutdown():
    pass
atexit.register(super)          # C caller, no Python frame at exit time

# 2. super() from a C callback: the "current frame" is the caller's frame,
#    not the callback's -- super() silently introspects an unrelated frame.
class Base:
    def probe(self):
        return "base"

class Derived(Base):
    def probe(self):
        # A __del__ fired here runs with THIS frame current.
        class Trap:
            def __del__(self):
                try:
                    s = super()          # zero-arg super() inside __del__
                    print("  __del__ super() ->", s, file=sys.stderr)
                except Exception as e:
                    print("  __del__ super() raised", type(e).__name__, e,
                          file=sys.stderr)
        Trap()                            # refcount drops immediately
        return super().probe()

print("Derived().probe() =", Derived().probe(), file=sys.stderr)

# 3. super() on a thread with a frame but no __class__ cell
def worker():
    try:
        super()
    except Exception as e:
        print("thread super() ->", type(e).__name__, e, file=sys.stderr)
t = threading.Thread(target=worker); t.start(); t.join()

print("about to exit (atexit super() runs now)", file=sys.stderr)
