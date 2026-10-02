# Release notes

## 0.4.0 (unreleased)

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

The private loop has a small idle CPU cost in local measurements. The subprocess
backend can create additional watcher and executor threads. The thread handoff
adds overhead to short RPC calls; use the asynchronous client for asynchronous
applications or high concurrency. Closing a connection releases its owned
resources but does not guarantee an immediate decrease in process RSS. Runtimes
are not shared between connections.

### Protocol and tool fixes

- Concurrent first calls load each tool once per connection.
- Cancellation and transport failures remove pending RPC requests. Response
  serialization failures return an error instead of leaving the caller waiting.
- Remote exception logs preserve traceback text across the connection.
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
