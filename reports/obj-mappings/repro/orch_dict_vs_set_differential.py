"""dict vs set differential for the mutate-during-__eq__ shape.

dict, Objects/dictobject.c compare_generic:1226-1235 --
    if (cmp < 0) return DKIX_ERROR;
    if (dk == mp->ma_keys && ep->me_key == startkey) { return cmp; }
    else { /* The dict was mutated, restart */ return DKIX_KEY_CHANGED; }
The revalidation gates the return of cmp for BOTH outcomes, so an "equal"
verdict from a mutating __eq__ still restarts.

set writer, Objects/setobject.c set_add_entry_takeref:290-295 --
    if (cmp > 0) goto found_active;                                  <- escapes first
    if (cmp < 0) goto comparison_error;
    if (table != so->table || entry->key != startkey) goto restart;  <- only cmp == 0

Same hazard, same fix already written twice elsewhere (dict's compare_generic
and set's own reader set_compare_entry_lock_held:159), missing on one path.
"""

import sys


class Vanish:
    """Removes itself from `container` during comparison, then claims equality."""

    def __init__(self):
        self.container = None
        self.fired = False

    def __hash__(self):
        return 12345

    def __eq__(self, other):
        if not self.fired and self.container is not None:
            self.fired = True
            try:
                if isinstance(self.container, set):
                    self.container.discard(self)
                else:
                    self.container.pop(self, None)
            except BaseException:  # noqa: BLE001
                pass
        return True


def probe_set():
    s = set()
    a = Vanish()
    s.add(a)
    a.container = s
    b = Vanish()
    s.add(b)  # must not raise
    return {"len": len(s), "new_present": b in s, "old_present": a in s}


def probe_dict():
    d = {}
    a = Vanish()
    d[a] = "old"
    a.container = d
    b = Vanish()
    d[b] = "new"  # must not raise
    return {"len": len(d), "new_present": b in d, "old_present": a in d}


def probe_frozenset_ctor():
    """The same hostile object through the frozenset constructor."""
    s = set()
    a = Vanish()
    s.add(a)
    a.container = s
    b = Vanish()
    try:
        fs = frozenset([a, b])
        return {"len": len(fs), "err": None}
    except BaseException as exc:  # noqa: BLE001
        return {"len": None, "err": repr(exc)}


def main():
    sr = probe_set()
    dr = probe_dict()
    fr = probe_frozenset_ctor()

    print("set  add():", sr)
    print("dict []=  :", dr)
    print("frozenset():", fr)
    print()

    set_lost = sr["len"] == 0 or not sr["new_present"]
    dict_lost = dr["len"] == 0 or not dr["new_present"]

    print(f"set  lost the element: {set_lost}")
    print(f"dict lost the element: {dict_lost}")
    print()
    if set_lost and not dict_lost:
        print("DIFFERENTIAL CONFIRMED: set loses it, dict does not.")
        return 7
    print("no differential")
    return 0


if __name__ == "__main__":
    sys.exit(main())
