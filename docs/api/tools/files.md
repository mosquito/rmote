# Files

`Files` provides file and directory handles on a connected POSIX host. It is
the backend for {doc}`SFTP through sshmux <../../sshmux>`, but you can use it
directly through either `Connection` or `Protocol`. The target needs Python
3.11+ and its standard library; rmote transfers the Tool code automatically.

Use `FileSystem` for operations by path, `Files` for open descriptors, and
{doc}`file_sync` or {doc}`rsync` for synchronization. `Files` writes directly
to the opened file. It does not stage an upload for atomic replacement.

## Open, read, and write

This runnable example uses a local Python subprocess as the target. Replace
`Connection.from_local()` with another {doc}`transport <../../transports>`
to reach a remote host; file paths then refer to that host.

<!-- name: test_files_handles -->
```python
import tempfile
from pathlib import Path

from rmote.sync import Connection
from rmote.tools import Files
from rmote.tools.files import OpenFlags

with tempfile.TemporaryDirectory() as directory:
    path = str(Path(directory) / "example.txt")
    with Connection.from_local() as remote:
        session_id = remote(Files.start)
        try:
            handle = remote(
                Files.open, session_id, path,
                OpenFlags.READ | OpenFlags.WRITE | OpenFlags.CREAT | OpenFlags.EXCL,
            )
            remote(Files.write, session_id, handle, 0, b"hello\n")
            assert remote(Files.read, session_id, handle, 0, 6) == b"hello\n"
            remote(Files.fsync, session_id, handle)
            remote(Files.close, session_id, handle)
        finally:
            remote(Files.release, session_id)
```

With async `Protocol`, use `await remote(Files.method, ...)` for each call.

## Session and descriptor ownership

`Files.start()` returns a new session ID. It also accepts a caller-generated
unique ID. Every subsequent operation requires that ID. File and directory
handles are opaque bytes, valid only in their owning session. A wrong session,
closed handle, or wrong handle type raises `OSError`; an unknown session does
not create one. This prevents accidental handle reuse across mux sessions.
It does not isolate paths or restrict the remote user's permissions.

`close` closes one handle. `release` closes all remaining file and directory
handles and removes the session; repeated release is safe. A file descriptor
continues to refer to the opened file after it is renamed or unlinked.
Keep the connection open until all operations and cleanup have finished.

A timeout or cancellation stops local waiting, not a remote file operation.
Before calling `release`, wait for outstanding operations, including `start`
and `open`, to complete. In async code, retain and shield the call task, then
await its completion during cancellation cleanup. Otherwise, a late `start`
can create a session after cleanup. The SFTP adapter handles this ordering.

Each session permits 128 handles. Calls in one session are serialized;
independent sessions can proceed concurrently. `read` and `write` use explicit
non-negative offsets and transfer at most 256 KiB per call. Reads can return
fewer bytes; empty bytes indicate EOF for a nonzero request. Writes complete
the supplied data before returning, but do not imply `fsync`.

`opendir` returns a directory handle. Call `readdir` until it returns an empty
list; each batch has at most 64 `(name, stat_result)` pairs. Entry order is
unspecified. Close the handle or release the session after iteration.

## Open flags

Import `OpenFlags` from `rmote.tools.files` and combine members with `|`.
These are SFTP values, not local `os.O_*` flags. `Files.open` translates them
on the target OS and also accepts integer wire values.

| Flag | Value | Effect |
| --- | ---: | --- |
| `READ` | `0x01` | Permit reads |
| `WRITE` | `0x02` | Permit writes |
| `APPEND` | `0x04` | Append writes, regardless of their supplied offset |
| `CREAT` | `0x08` | Create the file if absent |
| `TRUNC` | `0x10` | Truncate the file on open |
| `EXCL` | `0x20` | Fail if the file already exists |

At least `READ` or `WRITE` is required. `APPEND` and `TRUNC` require `WRITE`;
`TRUNC` and `EXCL` require `CREAT`. Unknown bits raise `ValueError`.
Only regular files can be opened; special files such as FIFOs are rejected.
Creation uses `mode` (default `0o666`) subject to the target process's umask.

`setstat` and `fsetstat` accept a dictionary with optional `size`, `permissions`,
`uid`/`gid`, and `atime`/`mtime` fields. Supply ownership and timestamp fields
in pairs. Times are Unix timestamps in seconds. These operations can partially
succeed before an OS error; they are not a transaction.

The SFTP adapter separately uses `AttrFlags` from `rmote.cli.sftp` to encode
attribute presence on the wire. Direct `Files` callers pass dictionaries and
do not need that enum. Extended SFTP attributes are unsupported.

## API reference

```{eval-rst}
.. autoclass:: rmote.tools.files.Files
   :members: start, release, open, close, read, write, stat, fstat, setstat, fsetstat, opendir, readdir, realpath, readlink, symlink, mkdir, remove, rename, fsync
```

```{eval-rst}
.. autoclass:: rmote.tools.files.OpenFlags
   :members:
```
