import threading

class Base:
    pass

def setter():
    for _ in range(5000):
        Base.__repr__ = lambda self: "x"

def subclasser():
    for _ in range(5000):
        type('Sub', (Base,), {})

threads  = [threading.Thread(target=setter)     for _ in range(2)]
threads += [threading.Thread(target=subclasser) for _ in range(4)]
for t in threads: t.start()
for t in threads: t.join()
print("done")
