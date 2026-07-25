/* CPY-0137: verify the arithmetic behind "dictresize's size guard is four bits
 * too permissive" -- Objects/dictobject.c:2200
 *
 *     if (log2_newsize >= SIZEOF_SIZE_T*8) { PyErr_NoMemory(); return -1; }
 *
 * admits log2_newsize == 63.  Reproduces USABLE_FRACTION and get_log2_bytes
 * verbatim from Objects/dictobject.c:590 and :803-822 and prints what
 * new_keys_object(63, ...) would compute at :865-867.
 *
 *   cc -std=c11 -O0 -o dictresize_size_guard dictresize_size_guard.c && ./dictresize_size_guard
 */
#include <stdio.h>
#include <stdint.h>
#include <stddef.h>

#define SIZEOF_VOID_P 8
#define PyDict_LOG_MINSIZE 3
/* Objects/dictobject.c:590 */
#define USABLE_FRACTION(n) (((n) << 1) / 3)

/* Objects/dictobject.c:803-822, verbatim */
static int
get_log2_bytes(uint8_t log2_size)
{
    int log2_bytes;
    if (log2_size < 8) {
        log2_bytes = log2_size;
    }
    else if (log2_size < 16) {
        log2_bytes = log2_size + 1;
    }
#if SIZEOF_VOID_P > 4
    else if (log2_size >= 32) {
        log2_bytes = log2_size + 3;
    }
#endif
    else {
        log2_bytes = log2_size + 2;
    }
    return log2_bytes;
}

int
main(void)
{
    printf("SIZEOF_SIZE_T*8 = %d  (the guard at :2200 rejects >= this)\n",
           (int)(sizeof(size_t) * 8));

    for (int l = 57; l <= 63; l++) {
        size_t n = (size_t)1 << l;
        size_t usable = USABLE_FRACTION(n);
        int lb = get_log2_bytes((uint8_t)l);
        printf("log2_newsize=%2d  1<<l=%20zu  USABLE_FRACTION=%20zu  "
               "get_log2_bytes=%3d  1<<log2_bytes %s\n",
               l, n, usable, lb,
               lb >= 64 ? "*** UNDEFINED (shift >= width) ***" : "ok");
    }

    /* The whole malloc argument at Objects/dictobject.c:865-867:
     *   sizeof(PyDictKeysObject) + ((size_t)1 << log2_bytes) + entry_size*usable
     * sizeof(PyDictUnicodeEntry) == 16, sizeof(PyDictKeysObject) == 40. */
    const size_t HDR = 40, ENTRY = 16;
    printf("\nsafe ceiling search over the FULL malloc argument\n"
           "  (1<<log2_bytes defined AND USABLE_FRACTION != 0 AND"
           " HDR + (1<<log2_bytes) + 16*usable does not wrap):\n");
    for (int l = 3; l <= 63; l++) {
        size_t n = (size_t)1 << l;
        size_t usable = USABLE_FRACTION(n);
        int lb = get_log2_bytes((uint8_t)l);
        int ub_def = lb < 64;
        int usable_ok = usable != 0;
        int prod_ok = usable_ok && usable <= (SIZE_MAX / ENTRY);
        int sum_ok = 0;
        if (ub_def && prod_ok) {
            size_t idx = (size_t)1 << lb;
            size_t ent = ENTRY * usable;
            sum_ok = (idx <= SIZE_MAX - HDR) && (ent <= SIZE_MAX - HDR - idx);
        }
        if (!(ub_def && usable_ok && prod_ok && sum_ok)) {
            printf("  first UNSAFE log2_newsize = %d  "
                   "(1<<log2_bytes defined=%d, usable!=0 =%d, 16*usable ok=%d,"
                   " sum ok=%d)\n",
                   l, ub_def, usable_ok, prod_ok, sum_ok);
            printf("  => safe ceiling is log2_newsize <= %d\n", l - 1);
            printf("  => the guard at :2200 admits %d..63, i.e. %d values too many\n",
                   l, 64 - l);
            break;
        }
    }
    return 0;
}
