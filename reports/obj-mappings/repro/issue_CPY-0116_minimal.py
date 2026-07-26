import sys

d = {}
for i in range(1000):
    d["k%d" % i] = i          # combined table, dk_nentries == 1000
for i in range(1, 1000):
    del d["k%d" % i]          # ma_used == 1, dk_nentries still 1000

it = reversed(d)              # di_pos = dk_nentries - 1 = 999

d.clear()                     # fresh PyDict_MINSIZE keys object
d["k0"] = 0                   # ma_used == 1 again -> staleness check passes

print("about to iterate; di_pos is stale at 999", file=sys.stderr)
for k in it:                  # reads DK_UNICODE_ENTRIES(k)[999] on a 5-slot table
    pass
print("survived", file=sys.stderr)
