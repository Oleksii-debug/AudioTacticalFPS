/* CARR-owned native launch entry. No shell, elevation, download, update or repair. */
#define UNICODE
#define _UNICODE
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>
#include "launcher_core.h"

#define CAP 32768
static wchar_t root[CAP], python[CAP], base_python[CAP], script[CAP], command[CAP];
static wchar_t environment_path[CAP], marker_path[CAP], scratch[CAP];
static wchar_t managed_parent[CAP];
static int quiet = 0;
static HANDLE managed_lease = INVALID_HANDLE_VALUE;

static void emit(const wchar_t *message, DWORD stream) {
    int bytes = WideCharToMultiByte(CP_UTF8, 0, message, -1, NULL, 0, NULL, NULL);
    HANDLE handle = GetStdHandle(stream);
    char *utf8; DWORD written;
    if (bytes < 1 || !handle || handle == INVALID_HANDLE_VALUE) return;
    utf8 = (char *)HeapAlloc(GetProcessHeap(), 0, (SIZE_T)bytes);
    if (!utf8) return;
    if (WideCharToMultiByte(CP_UTF8, 0, message, -1, utf8, bytes, NULL, NULL))
        WriteFile(handle, utf8, (DWORD)(bytes - 1), &written, NULL);
    HeapFree(GetProcessHeap(), 0, utf8);
}

static int fail(int code, const wchar_t *message) {
    if (quiet) { emit(message, STD_ERROR_HANDLE); emit(L"\n", STD_ERROR_HANDLE); }
    else MessageBoxW(NULL, message, L"Universal Research Library", MB_OK | MB_ICONERROR | MB_SETFOREGROUND);
    return code;
}

/* 1 = correct plain local object, 0 = absent, -1 = inaccessible/linked/wrong type. */
static int present(const wchar_t *path, int directory) {
    DWORD flags = GetFileAttributesW(path), error;
    if (flags == INVALID_FILE_ATTRIBUTES) {
        error = GetLastError();
        return error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND ? 0 : -1;
    }
    if (flags & FILE_ATTRIBUTE_REPARSE_POINT) return -1;
    return (!!(flags & FILE_ATTRIBUTE_DIRECTORY) == !!directory) ? 1 : -1;
}

static int child_path(const wchar_t *relative, int directory) {
    if (!rl_join(scratch, CAP, root, relative)) return -1;
    return present(scratch, directory);
}

/* The standard installer holds the same file exclusively for its complete
   transaction. Portable bundles have no parent marker and retain old behavior.
   Keep the read handle until the Python child exits; no PID lock or polling. */
static int acquire_managed_lease(void) {
    size_t length = rl_len(root), i;
    volatile wchar_t *copied_parent = managed_parent;
    if (length && root[length - 1] == L'\\') --length;
    i = length; while (i && root[i - 1] != L'\\') --i;
    if (length - i != 3 || !((root[i] == L'a' || root[i] == L'A') &&
        (root[i + 1] == L'p' || root[i + 1] == L'P') &&
        (root[i + 2] == L'p' || root[i + 2] == L'P'))) return 1;
    if (!i || i + 1 >= CAP) return 0;
    /* Preserve the explicit stores: this launcher deliberately has no CRT.
       MSVC otherwise lowers this copy to an unresolved memcpy call. */
    for (length = 0; length < i; ++length) copied_parent[length] = root[length];
    managed_parent[i] = 0;
    if (!rl_join(scratch, CAP, managed_parent, L"managed-install.ini")) return 0;
    if (present(scratch, 0) == 0) return 1;
    if (present(scratch, 0) != 1 || !rl_join(scratch, CAP, managed_parent, L"installation.lease") ||
        present(scratch, 0) != 1) return 0;
    managed_lease = CreateFileW(scratch, GENERIC_READ, FILE_SHARE_READ, NULL,
                               OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT, NULL);
    return managed_lease != INVALID_HANDLE_VALUE;
}

static int state(void) {
    int env = present(environment_path, 1);
    int scripts = child_path(L".venv\\Scripts", 1);
    if (present(root, 1) != 1 || scripts < 0 || child_path(L"runtime", 1) < 0) return RL_PATH_ERROR;
    return rl_decide(env, present(python, 0), present(marker_path, 0),
                     present(base_python, 0), present(script, 0));
}

static int set(const wchar_t *name, const wchar_t *value) {
    return SetEnvironmentVariableW(name, value) != 0;
}

static int prepend_path(const wchar_t *relative, const wchar_t *executable) {
    DWORD existing; wchar_t *combined; size_t used = 0, i;
    int available = child_path(executable, 0);
    if (available <= 0) return available == 0;
    if (!rl_join(scratch, CAP, root, relative)) return 0;
    existing = GetEnvironmentVariableW(L"PATH", NULL, 0);
    if (existing > CAP || rl_len(scratch) + existing + 2 >= CAP) return 0;
    combined = (wchar_t *)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, CAP * sizeof(wchar_t));
    if (!combined) return 0;
    for (i = 0; scratch[i]; ++i) rl_put(combined, CAP, &used, scratch[i]);
    if (existing) {
        rl_put(combined, CAP, &used, L';');
        if (!GetEnvironmentVariableW(L"PATH", combined + used, CAP - (DWORD)used)) {
            HeapFree(GetProcessHeap(), 0, combined); return 0;
        }
    }
    available = set(L"PATH", combined);
    HeapFree(GetProcessHeap(), 0, combined);
    return available;
}

static int setup_environment(void) {
    if (!set(L"ORT_DISABLE_TELEMETRY", L"1") || !set(L"HF_HUB_DISABLE_TELEMETRY", L"1") ||
        !set(L"HF_HUB_OFFLINE", L"1") || !set(L"TRANSFORMERS_OFFLINE", L"1") ||
        !set(L"DO_NOT_TRACK", L"1") || !set(L"CARR_PROGRAM_DIR", root)) return 0;
    if (!prepend_path(L"audio\\bin", L"audio\\bin\\ffmpeg.exe") ||
        !prepend_path(L"citation-runtime", L"citation-runtime\\node.exe")) return 0;
    if (child_path(L"ocr\\ocr-manifest.json", 0) == 1) {
        if (!rl_join(scratch, CAP, root, L"ocr") || !set(L"CARR_OCR_DIR", scratch)) return 0;
    }
    return 1;
}

static int begin_command(const wchar_t *executable, size_t *used) {
    *used = 0; command[0] = 0;
    return rl_arg(command, CAP, used, executable) && rl_arg(command, CAP, used, L"-I") &&
           rl_arg(command, CAP, used, L"-B") && rl_arg(command, CAP, used, L"-X") &&
           rl_arg(command, CAP, used, L"utf8");
}

static int run_child(const wchar_t *executable, int installer, DWORD *exit_code) {
    STARTUPINFOW startup; PROCESS_INFORMATION process;
    volatile unsigned char *p; size_t i;
    p = (unsigned char *)&startup; for (i = 0; i < sizeof(startup); ++i) p[i] = 0;
    p = (unsigned char *)&process; for (i = 0; i < sizeof(process); ++i) p[i] = 0;
    startup.cb = (DWORD)sizeof(startup);
    if (!CreateProcessW(executable, command, NULL, NULL, FALSE,
                        installer ? CREATE_NEW_CONSOLE : CREATE_NO_WINDOW,
                        NULL, root, &startup, &process)) return 0;
    CloseHandle(process.hThread);
    if (WaitForSingleObject(process.hProcess, INFINITE) != WAIT_OBJECT_0 ||
        !GetExitCodeProcess(process.hProcess, exit_code)) {
        CloseHandle(process.hProcess); return 0;
    }
    CloseHandle(process.hProcess); return 1;
}

static int run(void) {
    DWORD length, exit_code; int argc, mode = 0, current; wchar_t **argv; size_t used, index;
    argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (!argv) return fail(RL_ARGUMENT_ERROR, L"Windows could not read the launch arguments.");
    quiet = argc > 1;
    if (argc == 2) {
        if (rl_equal(argv[1], L"--self-test")) mode = 1;
        else if (rl_equal(argv[1], L"--check-installation")) mode = 2;
        else if (rl_equal(argv[1], L"--launcher-check")) mode = 3;
        else mode = -1;
    } else if (argc != 1) mode = -1;
    LocalFree(argv);
    if (mode < 0) return fail(RL_ARGUMENT_ERROR, L"Supported options: --launcher-check, --self-test, --check-installation.");
    length = GetModuleFileNameW(NULL, root, CAP);
    if (!length || length >= CAP) return fail(RL_PATH_ERROR, L"The program folder path is too long or unavailable.");
    if (!(length > 2 && root[1] == L':') &&
        !(length > 6 && root[0] == L'\\' && root[1] == L'\\' && root[2] == L'?' && root[5] == L':'))
        return fail(RL_PATH_ERROR, L"Extract the application into a local Windows drive folder.");
    index = length; while (index && root[index - 1] != L'\\') --index;
    if (!index) return fail(RL_PATH_ERROR, L"The application folder could not be determined.");
    root[index] = 0; /* Retaining the slash also preserves C:\\ correctly. */
    if (!acquire_managed_lease())
        return fail(RL_PROCESS_ERROR, L"Installation maintenance is in progress. Wait for setup or repair to finish, then open Resonance again.");
    if (!rl_join(python, CAP, root, L".venv\\Scripts\\python.exe") ||
        !rl_join(base_python, CAP, root, L"runtime\\python.exe") ||
        !rl_join(script, CAP, root, L"install_windows.py") ||
        !rl_join(environment_path, CAP, root, L".venv") ||
        !rl_join(marker_path, CAP, root, L".venv\\.installation-complete"))
        return fail(RL_PATH_ERROR, L"The program folder path is too long.");
    current = state();
    if (mode == 3) {
        if (current == RL_READY) emit(L"{\"schema\":1,\"state\":\"ready\"}\n", STD_OUTPUT_HANDLE);
        else if (current == RL_NEEDS_INSTALL) emit(L"{\"schema\":1,\"state\":\"needs-install\"}\n", STD_OUTPUT_HANDLE);
        else if (current == RL_INCOMPLETE) emit(L"{\"schema\":1,\"state\":\"incomplete\"}\n", STD_OUTPUT_HANDLE);
        else if (current == RL_MISSING) emit(L"{\"schema\":1,\"state\":\"missing-runtime\"}\n", STD_OUTPUT_HANDLE);
        else emit(L"{\"schema\":1,\"state\":\"invalid-path\"}\n", STD_OUTPUT_HANDLE);
        return current;
    }
    if (current == RL_PATH_ERROR)
        return fail(current, L"A program path is inaccessible, linked, or has an unexpected type. Use a complete local package.");
    if (current == RL_INCOMPLETE)
        return fail(current, L"Installation is incomplete. This launcher will not remove or replace the environment. Extract a verified package into a new program folder. Your existing research libraries remain available.");
    if (current == RL_MISSING)
        return fail(current, L"This package has no complete application or bundled Python runtime. Obtain the complete private Windows package. Research data has not been removed.");
    if (current == RL_NEEDS_INSTALL && mode)
        return fail(current, L"The application is not installed. Open ResearchLibrary.exe without diagnostic arguments to run the bundled offline installer.");
    if (!setup_environment()) return fail(RL_PATH_ERROR, L"The local runtime environment could not be configured.");
    if (current == RL_NEEDS_INSTALL) {
        if (!begin_command(base_python, &used) || !rl_arg(command, CAP, &used, script))
            return fail(RL_PATH_ERROR, L"The installer command exceeds the Windows limit.");
        if (!run_child(base_python, 1, &exit_code))
            return fail(RL_PROCESS_ERROR, L"The bundled offline installer could not start.");
        if (exit_code || state() != RL_READY)
            return fail(RL_INSTALL_FAILED, L"Offline installation did not complete. No existing environment was replaced. The installer console reported the problem; keep your research data and use a new verified program folder.");
    }
    if (!begin_command(python, &used) || !rl_arg(command, CAP, &used, L"-m"))
        return fail(RL_PATH_ERROR, L"The application command exceeds the Windows limit.");
    if (mode == 2) {
        if (!rl_arg(command, CAP, &used, L"carr_library.native_checks") ||
            !rl_arg(command, CAP, &used, L"--bundle-root") || !rl_arg(command, CAP, &used, root))
            return fail(RL_PATH_ERROR, L"The installation-check command is too long.");
    } else {
        if (!rl_arg(command, CAP, &used, L"carr_library")) return RL_PATH_ERROR;
        if (child_path(L"models\\minilm-manifest.json", 0) == 1) {
            if (!rl_arg(command, CAP, &used, L"--model-dir") ||
                !rl_join(scratch, CAP, root, L"models\\minilm") || !rl_arg(command, CAP, &used, scratch) ||
                !rl_arg(command, CAP, &used, L"--model-manifest") ||
                !rl_join(scratch, CAP, root, L"models\\minilm-manifest.json") || !rl_arg(command, CAP, &used, scratch))
                return fail(RL_PATH_ERROR, L"The local model paths exceed the Windows limit.");
        }
        if (mode == 1 && !rl_arg(command, CAP, &used, L"--self-test")) return RL_PATH_ERROR;
    }
    if (!run_child(python, 0, &exit_code))
        return fail(RL_PROCESS_ERROR, L"The installed application could not start. Keep this program folder and run the installation check; your research data has not been removed.");
    if (exit_code) return fail((int)exit_code, L"The application exited with an error. Run CheckInstallation.cmd for detailed runtime diagnostics. Your research data has not been removed.");
    return 0;
}

/* Explicit Win32 entry avoids a separately linked C runtime or DLL payload. */
void WINAPI launcher_entry(void) {
    UINT result = (UINT)run();
    if (managed_lease != INVALID_HANDLE_VALUE) CloseHandle(managed_lease);
    ExitProcess(result);
}
