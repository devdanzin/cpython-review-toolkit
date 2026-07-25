# Python Wrapper `__new__` Without `__init__` Safety Rule

Design document for detecting Python wrapper classes that break when
`__new__` is called without `__init__`.  Based on findings from the
CPython 3.14 tp_init/tp_new safety audit.

## The Bug

Python's object model allows two-phase construction:

```python
obj = SomeType.__new__(SomeType)   # allocates, does NOT call __init__
obj.__init__(args)                  # initializes
```

When a **Python class wraps a C extension type** and adds instance
attributes in `__init__`, calling `__new__` without `__init__` produces
an object that is valid at the C level but broken at the Python level —
methods that access the Python-set attributes crash with
`AttributeError`.

### Concrete Example: `socket.socket`

```python
import socket

# Two-phase construction
y = socket.socket.__new__(socket.socket)

# C-level is fine: sock_fd == INVALID_SOCKET, zeroed struct
print(y.fileno())  # -1 (safe)

# Python-level is broken: close() accesses self._io_refs
y.close()
# → AttributeError: 'socket.socket' object has no attribute '_io_refs'
```

**Why it happens:**

- `_socket.socket` (C type) — `sock_new` uses `tp_alloc` (zeroing),
  sets `sock_fd = INVALID_SOCKET`.  The C object is fully safe.

- `socket.socket` (Python wrapper in `Lib/socket.py`) — inherits from
  `_socket.socket` and adds in `__init__`:
  ```python
  def __init__(self, family=-1, type=-1, proto=-1, fileno=None):
      _socket.socket.__init__(self, family, type, proto, fileno)
      self._io_refs = 0      # ← only set here
      self._closed = False    # ← only set here
  ```

- `socket.socket.close()` assumes these exist:
  ```python
  def close(self):
      self._closed = True
      if self._io_refs <= 0:  # ← AttributeError if __init__ never ran
          self._real_close()
  ```

The C `_socket.socket.__new__` is perfectly safe.  The Python
`socket.socket.__new__` inherits that safety.  But the Python wrapper's
*methods* break because they depend on attributes that only `__init__`
sets.

## Why This Matters

### 1. It's a real bug class, not theoretical

Roger Binns (APSW maintainer) identified this pattern in CPython's own
stdlib.  The `socket.socket` example is trivially reproducible:

```python
import socket
y = socket.socket.__new__(socket.socket)
y.close()  # AttributeError
```

### 2. It affects the entire stdlib wrapper pattern

CPython's stdlib has many Python classes wrapping C types:

| Python class | C type | `__init__` sets |
|-------------|--------|-----------------|
| `socket.socket` | `_socket.socket` | `_io_refs`, `_closed` |
| `ssl.SSLSocket` | `_ssl._SSLSocket` | `_context`, `_sslobj`, `_connected` |
| `io.BufferedReader` | `_io.BufferedReader` | (may add Python attrs) |
| `io.TextIOWrapper` | `_io.TextIOWrapper` | (may add Python attrs) |
| `sqlite3.Connection` | `_sqlite3.Connection` | (may add Python attrs) |

Any Python wrapper that adds instance attributes in `__init__` and
accesses them in methods without `hasattr`/`getattr` guards is
vulnerable.

### 3. It's invisible to both C and Python analyzers

- **C analyzers** (cpython-review-toolkit) see `tp_new` using a zeroing
  allocator → "SAFE".  The C level *is* safe.
- **Python analyzers** (code-review-toolkit) analyzing `Lib/socket.py`
  in isolation see normal `__init__` attribute setting → no issue.
- **Neither analyzer** understands the cross-language boundary: the C
  `tp_new` creates a valid C object, but the Python wrapper's methods
  assume `__init__` ran.

### 4. It's distinct from the two C-level rules

The existing cpython-review-toolkit checks:

| Rule | Scope | What it catches |
|------|-------|----------------|
| `init_not_reinit_safe` | C tp_init | Re-init leaks C resources |
| `new_missing_member_init` | C tp_new | Garbage C pointers after new |
| **This rule** | **Python wrapper** | **Missing Python attrs after new** |

This third rule operates at the Python/C boundary — it requires
understanding *both* layers.

## Detection Strategy

### Where this check belongs

This is a **parity problem** — the C type and Python wrapper have
different assumptions about object state after `__new__`.  The
cext-review-toolkit's **parity-checker** agent is the natural home,
since it already compares C and Python implementations of the same
functionality.

Alternatively, code-review-toolkit could detect this purely from the
Python side, since the pattern is visible in Python code alone (attrs
set in `__init__`, accessed in methods without guards).

### What to detect

**Pattern**: A Python class that:
1. Inherits from a C extension type (directly or indirectly)
2. Sets instance attributes in `__init__` (`self.attr = ...`)
3. Has methods that access those attributes (`self.attr`) without
   guards (`hasattr`, `getattr` with default, try/except AttributeError)

**Guard patterns to recognize (NOT a bug)**:

```python
# Pattern 1: hasattr check
if hasattr(self, '_io_refs'):
    ...

# Pattern 2: getattr with default
refs = getattr(self, '_io_refs', 0)

# Pattern 3: try/except AttributeError
try:
    self._io_refs -= 1
except AttributeError:
    pass

# Pattern 4: __slots__ with defaults
class MySocket(socket.socket):
    _io_refs: int = 0      # class-level default
    _closed: bool = False   # class-level default

# Pattern 5: __init_subclass__ or __set_name__ that sets defaults

# Pattern 6: Attribute set in __new__ (not __init__)
def __new__(cls, ...):
    obj = super().__new__(cls)
    obj._io_refs = 0  # safe — set in __new__
    return obj
```

### Detection algorithm (Python-side, for code-review-toolkit)

```
For each class C that inherits from a C extension type:
  1. Collect INIT_ATTRS = {attr names assigned via self.attr = ... in __init__}
  2. Collect METHOD_ATTRS = {attr names accessed via self.attr in other methods}
  3. UNGUARDED = METHOD_ATTRS ∩ INIT_ATTRS - GUARDED_ATTRS
  4. If UNGUARDED is non-empty:
     → finding: "C.__new__ without __init__ will produce object missing
        {UNGUARDED}, causing AttributeError in {methods}"
```

Identifying "inherits from a C extension type" at static analysis time:
- Check if any base class is defined in a `.so`/`.pyd` (not in `.py`)
- For stdlib analysis: maintain a list of known C extension types
  (`_socket.socket`, `_ssl._SSLSocket`, `_io.BufferedReader`, etc.)
- Check `import _foo; class Foo(_foo.Bar)` patterns

### Detection algorithm (parity-side, for cext-review-toolkit)

The parity-checker already identifies dual C/Python implementations.
Extend it to:

```
For each (C_type, Python_wrapper) pair:
  1. Check C tp_new: does it fully initialize the object?
  2. Check Python __init__: does it set additional attributes?
  3. Check Python methods: do they access those __init__-set attributes
     without guards?
  4. If yes → the wrapper is not __new__-safe
```

This is more precise because the parity-checker already knows which
C type maps to which Python wrapper.

## Classification

- **FIX** if methods crash (AttributeError) or produce wrong results
  on an uninitialized object, AND the type is constructible via
  `__new__` (not blocked by `__new__` requiring arguments)
- **CONSIDER** if methods have partial guards but some code paths are
  unprotected
- **ACCEPTABLE** if the type blocks `__new__` without `__init__`
  (e.g., `__new__` requires mandatory arguments that also trigger
  `__init__`), or if the type is not intended to be subclassed

## Fixing the Bug

Several approaches, from least to most invasive:

### 1. Set defaults in `__new__` (best for simple cases)

```python
class socket(socket.socket):
    def __new__(cls, *args, **kwargs):
        obj = super().__new__(cls)
        obj._io_refs = 0
        obj._closed = False
        return obj
```

### 2. Guard attribute access in methods

```python
def close(self):
    self._closed = True
    if getattr(self, '_io_refs', 0) <= 0:
        self._real_close()
```

### 3. Use `__slots__` with class-level defaults

```python
class socket(_socket.socket):
    _io_refs: int = 0
    _closed: bool = False
```

Note: this changes the attribute storage model and may not be
backwards-compatible for subclasses.

### 4. Raise in `__new__` if type requires `__init__`

```python
def __new__(cls, *args, **kwargs):
    # Force callers to use normal construction
    if not args and not kwargs:
        raise TypeError("socket() requires arguments")
    return super().__new__(cls)
```

This is the most restrictive but clearest — it prevents the broken
state entirely.

## Scope of the Problem in CPython 3.14

From our audit, the C-level types are generally safe:
- Most use `tp_alloc` (zeroing) or explicitly NULL-init members
- `DISALLOW_INSTANTIATION` prevents `__new__` on many internal types
- The C tp_new + C tp_init path is well-tested

The risk concentrates in **Python wrappers in `Lib/`** that add
attributes in `__init__`.  A targeted scan of `Lib/*.py` for classes
inheriting from C types and setting `self.attr = ...` in `__init__`
would quantify the full scope.

## Relationship to Other Rules

| Rule | Layer | Detectable by |
|------|-------|--------------|
| `init_not_reinit_safe` | C | cpython-review-toolkit scanner |
| `new_missing_member_init` | C | cpython-review-toolkit scanner |
| **`wrapper_new_without_init`** | **Python/C boundary** | **parity-checker or Python analyzer** |
| Python `__init__` re-init | Python | code-review-toolkit (future) |

The first two rules catch C-level bugs.  This rule catches the
Python/C boundary bug.  A fourth rule (Python `__init__` re-init
without cleanup, purely in Python code) could also be added to
code-review-toolkit but is a different concern.

## Implementation Reference

For the cext-review-toolkit parity-checker:
- Extend `plugins/cext-review-toolkit/agents/parity-checker.md` with
  a new check: "Python wrapper `__new__` safety"
- No new script needed — the agent can grep `Lib/*.py` for the pattern

For code-review-toolkit:
- Could be added to `silent-failure-hunter` (it already finds missing
  error handling) or a new dedicated agent
- Script approach: use Python `ast` module to find `self.attr = ...`
  in `__init__` methods of classes inheriting from known C types,
  then check if methods access those attrs without guards
