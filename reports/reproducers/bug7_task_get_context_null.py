"""Bug 7: Task.get_context() missing NULL check after TaskObj_clear

_asynciomodule.c:2816 returns Py_NewRef(self->task_context) without
NULL check. TaskObj_clear (line 2421) sets task_context to NULL.

Strategy: Use a weak reference callback that fires during GC cycle
collection — after tp_clear has run on the task but before dealloc
completes. The callback calls get_context() on the partially-cleared
task.
"""

import asyncio
import gc
import weakref


results = []


class Poking:
    """Object that pokes task.get_context() when GC-collected."""

    def __init__(self, task):
        self.task_ref = weakref.ref(task)

    def __del__(self):
        task = self.task_ref()
        if task is not None:
            try:
                ctx = task.get_context()
                results.append(("ok", ctx))
            except SystemError as e:
                results.append(("SystemError", str(e)))
            except Exception as e:
                results.append((type(e).__name__, str(e)))


async def noop():
    pass


async def run():
    loop = asyncio.get_event_loop()
    task = loop.create_task(noop())
    await task

    print(f"Before: get_context() = {task.get_context()}")

    # Create a poking object in a reference cycle WITH the task.
    # When GC detects the cycle:
    # 1. tp_clear runs on task → sets task_context = NULL
    # 2. Poking.__del__ fires → calls task.get_context() → UAF/NULL deref
    poker = Poking(task)
    cycle = [task, poker]
    cycle.append(cycle)

    # Remove external references
    del task, poker

    gc.collect()
    gc.collect()

    if results:
        for kind, val in results:
            if kind == "SystemError":
                print(f"BUG CONFIRMED — SystemError during finalization: {val}")
            elif kind == "ok":
                if val is None:
                    print(f"get_context() returned None — bug fixed or context cleared")
                else:
                    print(f"get_context() returned {val} — tp_clear may not have run")
            else:
                print(f"{kind}: {val}")
    else:
        print("Poking.__del__ never fired — task not collected")
        print("(event loop likely holds a strong reference)")


print("=== Default task factory ===")
asyncio.run(run())

if hasattr(asyncio, "eager_task_factory"):
    results.clear()
    print("\n=== Eager task factory ===")
    loop = asyncio.new_event_loop()
    loop.set_task_factory(asyncio.eager_task_factory)
    loop.run_until_complete(run())
    loop.close()

# If neither approach triggers it, explain the issue:
if not results or all(r[0] == "ok" and r[1] is not None for r in results):
    print("\n--- Analysis ---")
    print("The event loop holds strong refs to tasks, preventing GC tp_clear.")
    print("The vulnerability is real (code inspection confirms missing NULL check)")
    print("but requires a specific scenario:")
    print("  1. Task completes")
    print("  2. Event loop releases the task (e.g., during loop.close())")
    print("  3. Task enters a reference cycle")
    print("  4. GC calls tp_clear (sets task_context = NULL)")
    print("  5. Code calls task.get_context() → Py_NewRef(NULL) → crash")
    print()
    print("This is the same pattern as the get_coro() fix (commit c086962).")
    print("The fix is: if (self->task_context == NULL) Py_RETURN_NONE;")
