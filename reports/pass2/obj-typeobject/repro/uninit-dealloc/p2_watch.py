# PASS-2 payload E: the type watcher / version-tag region (971-1481) plus
# type_dealloc's watcher-notify loop (:6988), which reads tp_watched as a
# scalar discriminator while tearing the type down.
class W3(W1):
    pass


_testcapi.watch_type(wid, W3)
W3.b = 2
W1.c = 3
del W1.c
W3.__bases__ = (W2,)
del W3  # -> type_dealloc with tp_watched set
import gc

gc.collect()
