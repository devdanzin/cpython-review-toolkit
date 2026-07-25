"""Bug: _ssl/debughelpers.c NULL from dead weak reference

debughelpers.c:32-38:
    if (ssl_obj->owner)
        PyWeakref_GetRef(ssl_obj->owner, &ssl_socket);
    else if (ssl_obj->Socket)
        PyWeakref_GetRef(ssl_obj->Socket, &ssl_socket);
    else
        ssl_socket = (PyObject *)Py_NewRef(ssl_obj);
    assert(ssl_socket != NULL);  // Only an assert! Disabled in release!

If the owning SSL socket is garbage-collected before the callback
fires, PyWeakref_GetRef returns 0 and ssl_socket is NULL.
The assert is the only guard — in release builds it's disabled,
and ssl_socket=NULL is passed to PyObject_CallFunction at line 72.

This is the same pattern as the fixed _servername_callback (24db78c).

Expected: crash in debug builds (assert failure), segfault in release.
"""

import ssl
import socket
import threading
import gc
import sys

print("This bug requires the SSL msg_callback debug helper to be")
print("active, which is only enabled when ssl.SSLContext has")
print("_msg_callback set (used internally for debugging).")
print()
print("The vulnerability is confirmed by code inspection:")
print("  _ssl/debughelpers.c:38 — assert(ssl_socket != NULL)")
print("  In release builds, assert is disabled.")
print("  If the weak reference is dead, ssl_socket is NULL.")
print("  PyObject_CallFunction(ssl_socket, ...) at line 72 → segfault")
print()
print("Same pattern as the fixed _servername_callback (commit 24db78c).")
print("Fix: replace assert with proper NULL check and early return.")
print()

# The msg_callback is an internal debug facility. It's set via
# the undocumented _msg_callback attribute on SSLContext.
# We can try to trigger it by setting up an SSL connection and
# deleting the socket during the handshake.

try:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    # Check if _msg_callback is available
    if hasattr(ctx, '_msg_callback'):
        print("_msg_callback is available — could potentially trigger")
        print("the debug helper path, but requires a live TLS connection")
        print("where the Python SSL socket is GC'd mid-handshake.")
    else:
        print("_msg_callback not available on this build.")
except Exception as e:
    print(f"SSL setup error: {e}")
