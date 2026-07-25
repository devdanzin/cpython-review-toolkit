"""find_name_in_mro:6147 — _PyObject_HashDictKey(name). Can `name` be anything
whose tp_hash descends a user object graph?
"""
import threading


def go():
    # 1. non-str name: rejected before any hash happens
    try:
        getattr(int, (1, 2))
    except TypeError as e:
        print("1 non-str name ->", type(e).__name__, e, flush=True)

    class S(str):
        def __hash__(self):
            return hash(S(self))          # unbounded self-recursion via Python

    # 2. str SUBCLASS with a recursive Python __hash__: re-enters the eval loop
    try:
        getattr(int, S("zz"))
    except RecursionError as e:
        print("2 str-subclass recursive __hash__ ->", type(e).__name__, e, flush=True)
    except AttributeError as e:
        print("2 str-subclass ->", type(e).__name__, e, flush=True)

    # 3. exact str: cached-hash fast path / unicode_hash (flat byte range)
    print("3 exact str ->", getattr(int, "bit_length", None) is not None, flush=True)


threading.stack_size(128 * 1024)
t = threading.Thread(target=go)
t.start()
t.join()
print("done", flush=True)
