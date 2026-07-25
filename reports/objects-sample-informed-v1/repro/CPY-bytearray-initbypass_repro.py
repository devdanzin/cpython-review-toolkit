# Objects/bytearrayobject.c — __new__ bypass leaves ob_bytes NULL; _PyBytes_Resize
# derefs it unguarded (bytearrayobject.c:280) -> SIGSEGV.
# main @ 3.16.0a0: SIGSEGV.  Released 3.14: returns bytearray(b'') cleanly => REGRESSION.
bytearray.__new__(bytearray).append(1)
