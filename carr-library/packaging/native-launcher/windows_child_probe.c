/* Synthetic private CI child. Never ship this executable in a product bundle. */
#define UNICODE
#define _UNICODE
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>
#include "launcher_core.h"
#define CAP 32768
static wchar_t root[CAP], path[CAP], source[CAP], value[CAP];

static void line(HANDLE file, const wchar_t *prefix, const wchar_t *text) {
    DWORD written;
    WriteFile(file, prefix, (DWORD)(rl_len(prefix) * sizeof(wchar_t)), &written, NULL);
    WriteFile(file, text, (DWORD)(rl_len(text) * sizeof(wchar_t)), &written, NULL);
    WriteFile(file, L"\n", sizeof(wchar_t), &written, NULL);
}

void WINAPI launcher_entry(void) {
    wchar_t **argv; int argc, install, i; HANDLE file; DWORD code = 0;
    const wchar_t *names[] = {L"CARR_PROGRAM_DIR", L"CARR_OCR_DIR", L"HF_HUB_OFFLINE", L"ORT_DISABLE_TELEMETRY", L"PATH"};
    argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (!argv || !GetCurrentDirectoryW(CAP, root)) ExitProcess(71);
    rl_join(path, CAP, root, L"install_windows.py");
    install = argc == 6 && rl_equal(argv[5], path);
    rl_join(path, CAP, root, install ? L"probe-install.txt" : L"probe-app.txt");
    file = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (file == INVALID_HANDLE_VALUE) ExitProcess(72);
    line(file, L"CWD=", root);
    for (i = 0; i < argc; ++i) line(file, L"ARG=", argv[i]);
    for (i = 0; i < 5; ++i) {
        value[0] = 0; GetEnvironmentVariableW(names[i], value, CAP);
        line(file, names[i], L"="); line(file, L"VALUE=", value);
    }
    CloseHandle(file);
    if (install) {
        rl_join(path, CAP, root, L".venv"); if (!CreateDirectoryW(path, NULL)) ExitProcess(73);
        rl_join(path, CAP, root, L".venv\\Scripts"); if (!CreateDirectoryW(path, NULL)) ExitProcess(74);
        if (!GetModuleFileNameW(NULL, source, CAP)) ExitProcess(75);
        rl_join(path, CAP, root, L".venv\\Scripts\\python.exe");
        if (!CopyFileW(source, path, TRUE)) ExitProcess(76);
        rl_join(path, CAP, root, L".venv\\.installation-complete");
        file = CreateFileW(path, GENERIC_WRITE, 0, NULL, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL);
        if (file == INVALID_HANDLE_VALUE) ExitProcess(77);
        CloseHandle(file);
    }
    value[0] = 0; GetEnvironmentVariableW(L"CARR_LAUNCHER_PROBE_EXIT", value, CAP);
    for (i = 0; value[i] >= L'0' && value[i] <= L'9'; ++i) code = code * 10 + (DWORD)(value[i] - L'0');
    LocalFree(argv);
    ExitProcess(code);
}
