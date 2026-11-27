#ifndef CARR_LAUNCHER_CORE_H
#define CARR_LAUNCHER_CORE_H
#include <stddef.h>
#include <wchar.h>

enum rl_state { RL_READY = 0, RL_INCOMPLETE = 3, RL_MISSING = 4,
                RL_PATH_ERROR = 5, RL_PROCESS_ERROR = 6, RL_INSTALL_FAILED = 7,
                RL_NEEDS_INSTALL = 10, RL_ARGUMENT_ERROR = 64 };

static size_t rl_len(const wchar_t *text) {
    size_t n = 0; while (text[n]) ++n; return n;
}

static int rl_equal(const wchar_t *a, const wchar_t *b) {
    size_t n = 0; while (a[n] && a[n] == b[n]) ++n; return a[n] == b[n];
}

static int rl_put(wchar_t *out, size_t capacity, size_t *used, wchar_t value) {
    if (*used + 1 >= capacity) return 0;
    out[(*used)++] = value; out[*used] = 0; return 1;
}

/* Microsoft CRT-compatible quoting. Every argument is quoted; no shell sees it. */
static int rl_arg(wchar_t *out, size_t capacity, size_t *used, const wchar_t *arg) {
    size_t i = 0, slashes;
    if (*used && !rl_put(out, capacity, used, L' ')) return 0;
    if (!rl_put(out, capacity, used, L'"')) return 0;
    while (arg[i]) {
        slashes = 0;
        while (arg[i] == L'\\') { ++slashes; ++i; }
        if (!arg[i] || arg[i] == L'"') slashes *= 2;
        while (slashes--) if (!rl_put(out, capacity, used, L'\\')) return 0;
        if (arg[i] == L'"' && !rl_put(out, capacity, used, L'\\')) return 0;
        if (arg[i] && !rl_put(out, capacity, used, arg[i++])) return 0;
    }
    return rl_put(out, capacity, used, L'"');
}

static int rl_join(wchar_t *out, size_t capacity, const wchar_t *root, const wchar_t *relative) {
    size_t used = 0, i;
    out[0] = 0;
    for (i = 0; root[i]; ++i) if (!rl_put(out, capacity, &used, root[i])) return 0;
    if (used && out[used - 1] != L'\\' && !rl_put(out, capacity, &used, L'\\')) return 0;
    for (i = 0; relative[i]; ++i) if (!rl_put(out, capacity, &used, relative[i])) return 0;
    return 1;
}

/* Existing environments are never replaced, repaired by deletion, or reinstalled. */
static int rl_decide(int environment, int python, int marker, int bundled, int installer) {
    if (environment < 0 || python < 0 || marker < 0 || bundled < 0 || installer < 0) return RL_PATH_ERROR;
    if (environment) return python && marker ? RL_READY : RL_INCOMPLETE;
    if (python || marker) return RL_INCOMPLETE;
    return bundled && installer ? RL_NEEDS_INSTALL : RL_MISSING;
}
#endif
