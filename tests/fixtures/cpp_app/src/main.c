#include "legacy.h"

int main(int argc, char **argv) {
    return argc > 1 ? validate(argv[1]) : 0;
}
