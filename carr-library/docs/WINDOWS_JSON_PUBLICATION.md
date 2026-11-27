# Atomic JSON publication with retained Windows readers

The Windows reader in `audio.jobs` opens bounded JSON snapshots with
`FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE`. That sharing mode alone
does not make Python's `os.replace` suitable for replacing an open destination:
its `MoveFileExW` operation can still return Windows error 5. The original native
child-process regression exposed this distinction.

The shared `atomic_json` writer now publishes its already-flushed staging file
through `replace_json_file`. On Windows the publisher uses
`SetFileInformationByHandle(FileRenameInfoEx)` with
`REPLACE_IF_EXISTS | POSIX_SEMANTICS`. The existing reader handle keeps its old
bytes while subsequent opens use the newly published file. There is one atomic
rename and no destination deletion or missing-name interval.

Publication is restricted to one checked local directory. The publisher retains
the source handle and a non-delete-shared directory handle, uses the Win32
absolute-name/NULL-root request form, and rechecks source/destination identities and
reparse/link guards before publication. The destination still cannot be a linked
or read-only file. The existing caller writer lock, staged-file fsync and
directory-sync policy remain in force.

Only explicit unsupported-operation errors permit an ordinary atomic rename
fallback, after the original directory and both file identities are rechecked.
Access, sharing, disk-space and other failures remain failures. A fallback which
cannot replace an open destination also fails; it never unlinks the destination,
copies over it or reports a successful save.

`model_manifest._read_json_snapshot` still requires the same current path
identity after reading. Thus a simultaneous approved-manifest replacement is
detected, and the hash of new bytes cannot authenticate an older parsed snapshot.
The less strict observational JSON reader may return its complete older snapshot.

## Evidence and references

`tests/storage/test_state_json_reader.py` retains the real ordinary-reader
negative control, demonstrates the legacy `os.replace` failure with an open
shared-delete handle on Windows, and launches an actual isolated child using the
same production JSON writer. The strict status and model-approval tests perform
real file replacement and retain their identity/refusal assertions. Additional
portable cases cover native request layout, descriptor cleanup, unsupported-only
fallback, changed files/directories, linked paths and failed-publication cleanup.

Linux source execution does not qualify the native Windows syscall. The existing
Windows source and installed gates must execute these cases without skips.

Primary API contracts:

- [Microsoft FILE_RENAME_INFORMATION flags and open-handle semantics](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information)
- [Microsoft FILE_RENAME_INFO destination and UTF-16 length](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_rename_info)
- [Microsoft SetFileInformationByHandle](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-setfileinformationbyhandle)
- [Microsoft FILE_INFO_BY_HANDLE_CLASS](https://learn.microsoft.com/en-us/windows/win32/api/minwinbase/ne-minwinbase-file_info_by_handle_class)
- [CPython issue 46003: MoveFileExW and open destination restrictions](https://bugs.python.org/issue46003)
- [Git's Windows rename implementation using an absolute name and NULL root](https://github.com/git/git/blob/master/compat/mingw.c)
