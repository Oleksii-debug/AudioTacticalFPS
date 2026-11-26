"""Run selected synthetic tests with native network syscalls denied by the kernel.

This adds process restrictions before third-party imports. Failure to install
the restrictions aborts the check; there is no fallback to an unrestricted run.
"""
import ctypes
import ctypes.util
import errno
import os
import stat
import sys


class ArgumentComparison(ctypes.Structure):
    # Public libseccomp scmp_arg_cmp ABI; SCMP_CMP_NE is 1.
    _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_int),
                ('datum_a', ctypes.c_uint64), ('datum_b', ctypes.c_uint64)]


def deny_network():
    # A pre-existing connected socket could still use write/writev. This runner
    # requires a fresh process and rejects inherited sockets before test imports.
    for entry in os.listdir('/proc/self/fd'):
        try:
            mode = os.fstat(int(entry)).st_mode
        except OSError as exc:
            if exc.errno == errno.EBADF:
                continue  # The directory listing's temporary descriptor closed.
            raise
        if stat.S_ISSOCK(mode):
            raise RuntimeError('Inherited socket descriptor; start a fresh verification process')
    library = ctypes.util.find_library('seccomp')
    if not library:
        raise RuntimeError('libseccomp is required; refusing unrestricted execution')
    lib = ctypes.CDLL(library, use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_rule_add.restype = ctypes.c_int
    lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
        ctypes.c_int, ctypes.c_uint, ctypes.POINTER(ArgumentComparison)]
    lib.seccomp_rule_add_array.restype = ctypes.c_int
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_load.restype = ctypes.c_int
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    context = lib.seccomp_init(0x7fff0000)  # Allow ordinary file/compute syscalls.
    if not context:
        raise RuntimeError('cannot create network-deny filter')
    denied = []
    try:
        for name in ('socket', 'connect', 'bind', 'listen', 'accept',
                     'accept4', 'sendto', 'sendmsg', 'sendmmsg',
                     'io_uring_setup', 'io_uring_enter', 'io_uring_register'):
            number = lib.seccomp_syscall_resolve_name(name.encode('ascii'))
            if number < 0:
                if name.startswith('io_uring_'):
                    raise RuntimeError('cannot resolve mandatory network-deny rule: ' + name)
                continue
            if lib.seccomp_rule_add(context, 0x00050000 | errno.EPERM, number, 0) != 0:
                raise RuntimeError('cannot install network-deny rule: ' + name)
            denied.append(name)
        # uv/Tokio uses an anonymous local pair for signal wakeups. Permit
        # AF_UNIX pairs only; network sockets/connect/send remain forbidden.
        pair_number = lib.seccomp_syscall_resolve_name(b'socketpair')
        comparison = ArgumentComparison(0, 1, 1, 0)  # domain != AF_UNIX
        if pair_number < 0 or lib.seccomp_rule_add_array(context,
                0x00050000 | errno.EPERM, pair_number, 1, ctypes.byref(comparison)) != 0:
            raise RuntimeError('cannot restrict socketpair to anonymous local IPC')
        denied.append('socketpair except AF_UNIX')
        if 'socket' not in denied or 'connect' not in denied or lib.seccomp_load(context) != 0:
            raise RuntimeError('network-deny filter not active; refusing test execution')
    finally:
        lib.seccomp_release(context)
    # Check the real C ABI, not Python socket hooks, before third-party imports.
    libc = ctypes.CDLL(None, use_errno=True)
    for family in (2, 10):  # AF_INET, AF_INET6
        ctypes.set_errno(0)
        descriptor = libc.socket(family, 1, 0)
        if descriptor >= 0:
            os.close(descriptor)
            raise RuntimeError('network filter failed its native socket self-check')
        if ctypes.get_errno() != errno.EPERM:
            raise RuntimeError('native socket self-check returned an unexpected result')
    pair = (ctypes.c_int * 2)()
    if libc.socketpair(1, 1, 0, pair) != 0:
        raise RuntimeError('anonymous local IPC is unavailable')
    try:
        os.write(pair[0], b'IPC')
        if os.read(pair[1], 3) != b'IPC':
            raise RuntimeError('anonymous local IPC self-check failed')
    finally:
        os.close(pair[0])
        os.close(pair[1])
    print('Kernel network-deny filter active:', ', '.join(denied), flush=True)


def main():
    if sys.platform != 'linux' or len(sys.argv) < 2:
        raise SystemExit('Use Linux and pass explicit synthetic test paths.')
    deny_network()
    os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    if sys.argv[1] == '--ci':
        from pathlib import Path
        import runpy
        runner = Path(__file__).with_name('ci_check.py')
        sys.argv = [str(runner), *sys.argv[2:]]
        runpy.run_path(str(runner), run_name='__main__')
        return 0
    # Product code must set telemetry controls before the native import.
    # The kernel filter prevents egress even if that product control regresses.
    import pytest
    return pytest.main(['-q', '--tb=short', *sys.argv[1:]])


if __name__ == '__main__':
    raise SystemExit(main())
