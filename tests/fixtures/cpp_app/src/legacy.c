#include <string.h>
#include "legacy.h"

#define CHECK_LEN(s) (strlen(s) > 3)

static int checksum(const char *code) {
    int sum = 0;
    for (const char *p = code; *p; ++p) sum += *p;
    return sum;
}

int validate(const char *code) {
    return CHECK_LEN(code) && checksum(code) % 7 == 0;
}
