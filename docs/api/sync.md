# Synchronous Connection

Import `Connection` from `rmote.sync` or directly from `rmote`.
Create a connection with `from_local` or `from_ssh`. Direct construction
with `Connection()` raises `TypeError`.

```{eval-rst}
.. py:class:: Connection
   :module: rmote.sync

   Own a subprocess, a protocol, and a private background event loop.
   Use a factory and close the connection after its callers finish.

   .. automethod:: rmote.sync.Connection.from_local

   .. automethod:: rmote.sync.Connection.from_ssh

   .. automethod:: rmote.sync.Connection.__call__

   .. automethod:: rmote.sync.Connection.call_with_timeout

   .. automethod:: rmote.sync.Connection.uncompressed

   .. automethod:: rmote.sync.Connection.close

   .. automethod:: rmote.sync.Connection.__enter__

   .. automethod:: rmote.sync.Connection.__exit__
```

## Deadlines

Both factories accept these keyword-only arguments:

* `connect_timeout=30.0` limits process creation, bootstrap, and handshake.
* `rpc_timeout=None` sets the default deadline for each Tool call.
* `close_timeout=5.0` limits graceful protocol and process shutdown.

`None` disables a connect or RPC deadline. Numeric deadlines must be finite
and positive. `close_timeout` must be finite and positive; it cannot be
`None`. Invalid values raise `ValueError` before resource creation.

`connection.call_with_timeout(timeout, tool, /, *args, **kwargs)` overrides
the default RPC deadline for one call. It includes the first Tool upload,
packet transmission, and response. All keyword arguments go to the Tool
method, including arguments named `timeout` or `tool`.

`connection.uncompressed(tool, /, *args, **kwargs)` calls without
compressing the request or the response. Use it for data that cannot
shrink, such as an archive or an image: those bytes then skip the
dictionary of the connection instead of paying a deflate pass that gains
nothing. The call keeps the default deadline, every argument belongs to
the Tool method, and a neighbouring call keeps its own policy. See
{ref}`the frame codec <frame-compression>`.

Timeout raises `TimeoutError`. `KeyboardInterrupt` propagates unchanged.
Both stop local waiting; the remote operation can continue. Cancellation
during packet transmission can make the connection unusable. Close it and
create a new connection after a transport failure.

## Ownership and concurrency

Each connection owns one subprocess, one protocol, and one private event loop
in a background thread. Factories return after the handshake completes.
Factory failure or interruption cleans up partially created resources.

Use `with` or call `close()` in `finally`. Closing rejects new calls,
closes the channel, reaps the process, and joins the background thread.
After `close_timeout`, process cleanup uses terminate, a one-second grace
period, then kill and wait. This is not a fixed total limit for `close()`:
local cancellation and executor shutdown require cooperative code.

Repeated `close()` calls are safe. Concurrent closes wait for the same
cleanup. Nested context entry and calls after closing raise `RuntimeError`.
Context exit preserves an exception raised by the body; cleanup errors are
logged when a body exception already exists.

Multiple caller threads can share an open connection. Remote log records run
local logging handlers in a delivery thread, so the time a handler takes does
not limit the throughput of calls. Handlers must be thread safe and must not
call this connection's synchronous methods, because `close()` waits for the
records that are still queued. Calls from the loop thread of the connection
raise `RuntimeError`.

The throughput of one connection flattens as caller threads are added, because
the interpreter lock serializes the Python work of every call. On a local
transport with a tool that returns at once, one connection served 4 500 calls
per second from one thread, 15 900 from eight, 19 000 from sixteen, 21 500 from
thirty-two and 23 400 from sixty-four; the next doubling added 4 percent. The
CPU the loop thread spends per call falls the whole way, from 62 to 30
microseconds, so nothing collapses past the knee: a wider pool simply stops
paying off. Open a second connection when one is not enough.

In async applications, use `Protocol` or move the complete synchronous
lifecycle into `asyncio.to_thread`. A direct synchronous call blocks the
calling event loop. Shared runtimes, automatic reconnect, and remote
cancellation are not supported.

See {doc}`../quickstart`, {doc}`../multi-host`, and {doc}`../writing-tools`
for executable examples and Tool definitions.
