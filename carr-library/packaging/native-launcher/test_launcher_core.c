#include <assert.h>
#include <stdio.h>
#include "launcher_core.h"

int main(void) {
    wchar_t command[256], path[256]; size_t used = 0;
    assert(rl_arg(command, 256, &used, L"C:\\Program Files\\Київ\\python.exe"));
    assert(rl_arg(command, 256, &used, L"-I"));
    assert(rl_equal(command, L"\"C:\\Program Files\\Київ\\python.exe\" \"-I\""));
    used = 0; assert(rl_arg(command, 256, &used, L"a\\\"b\\"));
    assert(rl_equal(command, L"\"a\\\\\\\"b\\\\\""));
    used = 0; assert(rl_arg(command, 256, &used, L"")); assert(rl_equal(command, L"\"\""));
    assert(rl_len(command) == 2);
    used = 0; assert(!rl_arg(command, 4, &used, L"oversized"));
    assert(rl_join(path, 256, L"C:\\", L".venv\\Scripts\\python.exe"));
    assert(rl_equal(path, L"C:\\.venv\\Scripts\\python.exe"));
    assert(rl_join(path, 256, L"C:\\With spaces", L"runtime\\python.exe"));
    assert(rl_equal(path, L"C:\\With spaces\\runtime\\python.exe"));
    assert(rl_decide(1, 1, 1, 0, 0) == RL_READY);
    assert(rl_decide(1, 1, 0, 1, 1) == RL_INCOMPLETE);
    assert(rl_decide(1, 0, 1, 1, 1) == RL_INCOMPLETE);
    assert(rl_decide(0, 0, 0, 1, 1) == RL_NEEDS_INSTALL);
    assert(rl_decide(0, 0, 0, 0, 1) == RL_MISSING);
    assert(rl_decide(0, 0, 0, 1, 0) == RL_MISSING);
    assert(rl_decide(-1, 1, 1, 1, 1) == RL_PATH_ERROR);
    assert(rl_decide(0, 1, 0, 1, 1) == RL_INCOMPLETE);
    puts("PASS: quoting, Unicode paths, command bounds and installation lifecycle");
    return 0;
}
