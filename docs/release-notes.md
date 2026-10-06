# Release notes

## 0.7.0

- Unified `rmote` / `python -m rmote` CLI with short options. Replace
  `rmote-shell` with `rmote shell`; imports move to `rmote.cli.shell`.
- [SSH multiplexing](sshmux.md): OpenSSH shell/exec sessions share one rmote
  connection; `--daemon` supports automatic startup from SSH config.
- [Python REPL](repl.md) with host facts and tools, plus `--async` for `await`.
  `Connection.from_command` adds arbitrary transports to the synchronous API.
- [Synchronization CLI](rsync.md) with transfer logs, exclusions and optional
  deletion, including `--delete-excluded`. The remote host needs only Python.
- FileSync runs independent transfers concurrently; Rsync now defaults to
  32 concurrent files. Adjust this with `-j` or the API's `concurrency` argument.
- Bootstrap failures preserve transport diagnostics.
- A shared [transport guide](transports.md) covers SSH, Docker, Kubernetes,
  local Python and prepared streams such as `nc` relays.

## 0.6.0

### New features

- [Streaming RPC and module tools](writing-tools.md): typed async iterators,
  bounded buffering, cancellation and lazy transfer of Python modules/packages.
- [Interactive shell](shell.md): `rmote-shell` and `Vty` support POSIX terminals
  and pipes over SSH, Docker or other command transports.
- [Host facts](api/tools/facts.md): typed collectors for system, network,
  packages and services, with local or remote collection.
- [Result caching](api/cache.md): explicit freshness checks and JSON/SQLite
  persistence; [immutable snapshots](api/immutable.md) are available separately.
- [Portable templates](templating.md): Jinja-like syntax and compiled programs
  with transferable filters, without a Jinja2 dependency on the remote host.
- [Transport](api/protocol.md): interleaved frames, connection-history zlib
  compression and per-call `uncompressed(...)`. File transfers batch work;
  remote logs are batched and delivered outside the RPC event loop.

### Compatibility changes

- Rewrite old template syntax using `{{ ... }}` and `{% ... %}`. Import
  `Template` and `render_template` from `rmote.templates`, not `rmote.protocol`.
  Replace the old `rmote.tools.Template` tool with `RenderTemplate`;
  `render` accepts text or a compiled template and replaces `render_compiled`.
  `rmote.tools.template.Template` now refers to the engine, not the tool.
- Import `process` from `rmote.process`; `async_process` is available there too.
  Direct local calls to command-running methods of `Exec`, `Apt`, `Pacman`,
  `Service` and `User` now require `await`. RPC call syntax is unchanged.
- Unsupported platforms or missing tool prerequisites now raise
  `NotImplementedError`; catching `OSError` alone no longer covers these cases.
- `FileSync` and `Rsync` default to 4 MiB blocks, up from 1 MiB.
  `Connection.from_local`, `Connection.from_ssh` and `Protocol.from_ssh` now
  capture `stderr` through `PIPE` instead of discarding it with `DEVNULL`.
- Pass the callable to `Protocol` positionally, as already required by
  `Connection`; a `tool=` keyword is forwarded to the remote method.
  `Connection` rejects streaming calls with `TypeError` and calls from its
  log-delivery thread with `RuntimeError`.
- The wire format changed: `COMPRESSED` applies to individual frames and
  `rmote.protocol.LogRecord` is a tuple instead of a `TypedDict`. Independently
  started peers must use matching versions; normal connections bootstrap theirs.

Python 3.11+ remains required. The remote bootstrap uses only the standard library;
individual tools still require their platform facilities and permissions.

## 0.5.0 — 2026-10-03

This release adds file and directory synchronization over the existing Python
connection, expands filesystem and Linux configuration tools, and switches
protocol compression to gzip.

### File and directory synchronization

`FileSync.upload` and `FileSync.download` synchronize one file in either
direction. They compare SHA-256 hashes of fixed-size blocks and send only
changed content. The receiver reuses matching destination blocks, verifies the
completed file and installs it with an atomic replacement. The result reports
whether the file changed and how many bytes were transferred or reused.

Existing destination permissions and ownership are preserved; new files use
mode `0600`. Timestamps, ACLs and extended attributes are not copied. Transfers
need temporary space for the complete file, even when little content changes.
See the [FileSync API](api/tools/file_sync.md).

`Rsync.upload` and `Rsync.download` extend this to directory contents, including
empty directories and symbolic links. They support exclusion patterns,
permission preservation, optional destination ownership and optional removal
of extra entries with `delete=True`. Excluded entries remain untouched, and
symlinks are copied without following them. No external `rsync` executable is
required. Replacement is atomic per file; the whole directory operation is not
a transaction. See the [Rsync API](api/tools/rsync.md).

Call these upload/download helpers directly with an open asynchronous
`Protocol`, for example `await FileSync.upload(remote, source, destination)`.
They coordinate the transfer locally through ordinary Tool RPCs.

### Filesystem and system configuration

- `FileSystem` gains idempotent `write`, `directory`, `symlink` and `absent`
  operations, plus `stat` returning a typed `StatResult`. File and directory
  operations can manage permissions and ownership.
- `Hostname` reads and sets the Linux hostname, persists changes to
  `/etc/hostname`, and manages entries in `/etc/hosts`.
- `Sysctl` reads kernel parameters and applies values at runtime and in
  `/etc/sysctl.d`. It supports individual settings and convergence of a group
  of settings. Mutating system configuration requires appropriate privileges.

### Protocol and documentation

Packet and bootstrap compression use gzip instead of LZMA to reduce CPU cost.
SSH and subprocess connections bootstrap the matching protocol implementation;
independently started peers must use the same compression format.

API documentation moves to Markdown with Sphinx, MyST and autodoc. Runnable
examples cover the built-in tools and file transfers; system-changing examples
run in disposable Docker containers. Systemd test containers disable binfmt
handling so their startup cannot clear the host's registered handlers.

[Full changelog](https://github.com/mosquito/rmote/compare/0.4.1...0.5.0).

## 0.4.1 — 2026-10-02

Documentation links in the README and package metadata now point to
[docs.rmote.org](https://docs.rmote.org). The README documentation badge now
shows the status of the documentation build.

[Full changelog](https://github.com/mosquito/rmote/compare/0.4.0...0.4.1).

## 0.4.0 — 2026-10-02

This release adds a synchronous client over the existing asynchronous protocol.
The asynchronous API remains available. Both clients support synchronous and
asynchronous `Tool` methods, without runtime dependencies or an installed rmote
package on the remote host.

### Synchronous connections

Import `Connection` from `rmote.sync`. Create a local subprocess with
`Connection.from_local()` or an SSH connection with `Connection.from_ssh(host)`.
Both factories complete the handshake before they return. Use a context manager
or call `close()` to release the connection, subprocess, and background runtime.

Call a tool with `connection(tool, *args, **kwargs)`. Use
`connection.call_with_timeout(seconds, tool, *args, **kwargs)` to set a deadline
for one call. Factories also accept a default `rpc_timeout`. Tool parameter and
result types are preserved for both synchronous and asynchronous methods.

Each connection owns an event loop in a background thread. Multiple caller
threads can share a connection, and remote logs are delivered while callers are
idle. Logging handlers run on the background thread and must return promptly.
Calling the blocking client from its own runtime thread raises `RuntimeError`.
Use the asynchronous API in event loop code to avoid blocking that loop.

See the [quickstart](quickstart.md), [tool guide](writing-tools.md), and
[multi-host guide](multi-host.md) for complete examples.

### Timeouts and resource ownership

A call deadline includes tool loading, sending, and waiting for the result.
Timeouts and `KeyboardInterrupt` stop local waiting and clean up pending requests.
They do not cancel remote work. A late reply does not prevent subsequent calls;
interruption during a partial packet send closes the connection to preserve
protocol framing. Reconnection is explicit.

A new call after an already detected transport failure raises `ConnectionError`,
with the original error as its cause. Calls that were waiting when the transport
failed retain the original transport error.

`close()` is idempotent and coordinates concurrent callers. Shutdown closes the
protocol and escalates subprocess termination when necessary. `close_timeout`
controls the initial graceful shutdown wait, not a hard deadline for all cleanup.
Event loop shutdown requires cooperative tasks and completion of executor work.

### Protocol and tool fixes

- Concurrent first calls load each tool once per connection.
- Cancellation and transport failures remove pending RPC requests. Response
  serialization failures return an error instead of leaving the caller waiting.
- Remote exception logs preserve traceback text across the connection.
- `Exec.command` and `Exec.shell` discard output by default. Pass
  `capture_output=True` to receive stdout and stderr as bytes.
- Process tools accept text input correctly when text mode is enabled.
- Template expressions use Python tokenization for expression boundaries.
- Package state handling is consistent across Apt and Pacman; Apt reads current
  package status for each operation.
- User tools compare and update existing user IDs, groups, and GECOS fields.
- Authorized key insertion separates existing unterminated lines, and
  `line_in_file` avoids duplicate lines when its regular expression does not match.

### Compatibility

Python 3.11 or later is required. The wire protocol and autonomous bootstrap use
the same asynchronous implementation as before. The synchronous runtime is local
to the client and is not part of the remote bootstrap. This release adds no
automatic reconnect, shared runtime, or remote cancellation protocol.
