import re
p = '/home/danzin/projects/cpython/Include/internal/pycore_slots_generated.h'
src = open(p).read()
m = re.search(r'_PySlot_get_duplicate_handling\(uint16_t slot_id\)\s*\{(.*?)\n\}', src, re.S)
body = m.group(1)
dep = body.split('return _PySlot_PROBLEM_DEPRECATED;')[0]
allow = body.split('return _PySlot_PROBLEM_DEPRECATED;')[1].split('return _PySlot_PROBLEM_ALLOW;')[0]
for name in ('Py_tp_doc', 'Py_tp_members', 'Py_tp_alloc', 'Py_tp_basicsize',
             'Py_tp_extra_basicsize', 'Py_tp_itemsize', 'Py_tp_name',
             'Py_tp_token', 'Py_tp_flags', 'Py_tp_metaclass', 'Py_tp_module'):
    tier = ('DEPRECATED' if ('case %s:' % name) in dep
            else 'ALLOW' if ('case %s:' % name) in allow
            else 'REJECT (default)')
    print('%-24s -> %s' % (name, tier))
