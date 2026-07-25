/* CPY-0139: LOAD_INDEX / STORE_INDEX (Objects/dictobject.c:182-183 vs :271-272)
 * carry a trailing `;` in the FREE-THREADED arm only, so they are expression
 * macros under the GIL and statement macros under Py_GIL_DISABLED.
 *
 *   cc -std=c11 -fsyntax-only -DARM_GIL load_index_semicolon.c   # compiles
 *   cc -std=c11 -fsyntax-only -DARM_FT  load_index_semicolon.c   # syntax error
 */
#include <stdint.h>

struct keys { char dk_indices[64]; };

static inline int8_t _Py_atomic_load_int8_relaxed(const int8_t *p) { return *p; }
static inline void _Py_atomic_store_int8_relaxed(int8_t *p, int8_t v) { *p = v; }

#ifdef ARM_FT
/* Objects/dictobject.c:182-183 -- note the trailing ';' */
#define LOAD_INDEX(keys, size, idx) _Py_atomic_load_int##size##_relaxed(&((const int##size##_t*)keys->dk_indices)[idx]);
#define STORE_INDEX(keys, size, idx, value) _Py_atomic_store_int##size##_relaxed(&((int##size##_t*)keys->dk_indices)[idx], (int##size##_t)value);
#else
/* Objects/dictobject.c:271-272 -- no trailing ';' */
#define LOAD_INDEX(keys, size, idx) ((const int##size##_t*)(keys->dk_indices))[idx]
#define STORE_INDEX(keys, size, idx, value) ((int##size##_t*)(keys->dk_indices))[idx] = (int##size##_t)value
#endif

/* (a) statement context -- what all 8 in-tree call sites use.  Compiles in
 *     BOTH arms; the FT arm merely emits a stray null statement. */
int statement_context(struct keys *keys, int i)
{
    int ix;
    ix = LOAD_INDEX(keys, 8, i);
    STORE_INDEX(keys, 8, i, 3);
    return ix;
}

/* (b) expression context -- the latent break.  Compiles under the GIL arm,
 *     hard syntax error under the FT arm. */
int expression_context(struct keys *keys, int i)
{
    return (int)(LOAD_INDEX(keys, 8, i) + 1);
}
