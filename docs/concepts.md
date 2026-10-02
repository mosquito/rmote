# Concepts

## Client Interfaces and Resource Ownership

The synchronous {class}`~rmote.sync.Connection` wraps the same asynchronous
{class}`~rmote.protocol.Protocol` used by async applications. Tool serialization,
packet framing, and remote execution are shared. The remote bootstrap does not
include the synchronous client or its background runtime.

```{mermaid}
flowchart LR
    S["Synchronous caller threads"] --> B["Connection: private background loop"]
    A["Async application loop"] --> P["Protocol"]
    B --> P
    P <-->|"Existing packet stream"| R["Remote Protocol and Tool methods"]
```

| Resource | Synchronous `Connection` | Async `Protocol` |
|---|---|---|
| Local event loop | Private loop in one background thread | Application's running loop |
| Subprocess | Owned by the connection | `from_ssh` owns SSH; `from_subprocess` leaves ownership with its caller |
| Handshake | Completed before a factory returns | Completed when entering the protocol context |
| Cleanup | `with` or explicit `close()` | `async with`, plus cleanup for caller-owned processes |

One synchronous connection owns one subprocess, protocol, and background thread.
There is no global or shared runtime. Multiple external threads can call one
open connection; results are matched by request ID. Do not close it until its
callers finish unless you intend to interrupt their pending requests.

The synchronous factories clean up failed or interrupted startup. Context exit
closes the connection on success, operation failure, or `KeyboardInterrupt`.
Explicit `close()` rejects new calls, closes the channel, reaps its process, and
joins the thread. Repeated close is safe. Nested context entry and operations
after closing raise `RuntimeError`.

See {doc}`writing-tools` for both client styles and {doc}`api/sync` for deadlines.

## Cancellation and Logging

Synchronous RPC deadlines include first Tool upload, packet transmission, and
response waiting. Timeout raises `TimeoutError`; `KeyboardInterrupt` propagates.
Both cancel local waiting and remove the pending request. Async callers can use
`asyncio.timeout` around an operation to request the same local cancellation.

Local cancellation does not cancel remote work. No remote cancellation packet
is sent. Remote side effects can still occur. Late responses for cancelled or
unknown request IDs are ignored. Cancellation while sending a packet can leave
an incomplete frame and close the channel. A transport failure requires closing
the connection and creating another; automatic reconnect is not available.

The close deadline limits graceful process shutdown. Cleanup then uses terminate,
a one-second grace period, and kill. Total cleanup can take longer because
local cancellation and executor shutdown require cooperative code.

Remote log records invoke local logging handlers in the connection's background
loop thread. Keep handlers thread safe. A handler must not call synchronous
methods of that connection; these calls raise `RuntimeError` from its own loop
thread. The loop remains active between RPCs, so background events can arrive
while the caller is idle.

Use `Protocol` in async applications. Alternatively, use `asyncio.to_thread`
for the complete synchronous lifecycle. A direct synchronous call blocks the
calling loop. Cancelling a `to_thread` await does not stop its worker thread.

## Bootstrap Flow

When {meth}`~rmote.protocol.BaseProtocol.from_subprocess` is called, rmote injects its entire
protocol implementation into the remote Python interpreter as a compressed, base64-encoded payload:

```text
exec(decompress(b64decode("...")))   # injects protocol.py
asyncio.run(run())                   # starts event loop
```

The remote process then writes `PROTOCOL READY\n` to stdout; the local side waits for this
boundary before starting the packet exchange.

```{mermaid}
sequenceDiagram
    participant Local
    participant Remote

    Local->>Remote: Bootstrap (exec compressed payload)
    Remote-->>Local: PROTOCOL READY
    Local->>Remote: SYNC Tool (tool_to_dict)
    Remote-->>Local: ACK
    Local->>Remote: RPC call
    Remote-->>Local: Result
```

## Wire Protocol

### Packet Structure

Every message is framed with a fixed-size 21-byte header (struct `">5sIIQ"`) followed by a
pickled payload:

```{mermaid}
packet-beta
  0-39:   "magic - b'RMOTE' (5 bytes)"
  40-71:  "flags - Flags IntFlag (4 bytes)"
  72-103: "length - payload size uint32 (4 bytes)"
  104-167: "packet_id - request correlator uint64 (8 bytes)"
```

The payload is `pickle.dumps(data)`, lzma-compressed when its size exceeds 1024 bytes (the
`COMPRESSED` flag is set in that case).

### Flags

The `flags` field is a combination of {class}`~rmote.protocol.Flags` values:

| Flag         | Value | Meaning                              |
|--------------|-------|--------------------------------------|
| `COMPRESSED` | 1     | Payload is lzma-compressed           |
| `REQUEST`    | 2     | Sender expects a response            |
| `RESPONSE`   | 4     | This is a response to a request      |
| `SYNC`       | 8     | Tool synchronization                 |
| `RPC`        | 16    | Remote procedure call                |
| `EXCEPTION`  | 32    | Response carries an exception        |
| `LOG`        | 64    | Log record forwarded from remote     |

## Tool Serialization Lifecycle

Tools are transferred lazily and cached for the lifetime of the connection:

1. `ToolMeta` records class source for the inline-tool fallback. For a class
   defined at module level, `tool_to_dict` reads the whole module's source.
2. The first call sends that source in a `SYNC | REQUEST` packet. The receiver
   executes it in a module namespace and registers the module in `sys.modules`.
3. The receiver constructs a tool instance and stores it in `Protocol.tools`.
   Module tools use `module.ClassName` as their key. Inline tools use the bare
   class name, so two inline tools with the same name can collide.
4. Concurrent first calls share loading locks. Classes from one module reuse
   that module's namespace. Later calls send an RPC packet without another
   successful upload.

Opening a connection does not upload tools. Each new connection has a new
local cache and loads tools on first use. Editing local source does not update
an already loaded remote module. See {doc}`writing-tools` for source layout,
dependencies, and custom return types.

## Standard Streams and Trust

The remote bootstrap duplicates the descriptors used for protocol traffic and
redirects standard input, output, and error to `/dev/null`. The protocol copies
are not inherited by child commands. Remote `print()` and uncaptured command
output are discarded; use return values or logging to send information back.

Tool source executes on the target, and messages contain pickled Python values.
Use rmote only with trusted peers and code. The packet format does not provide
authentication or encryption; use a trusted local process or an SSH transport.

## Concurrency Model

rmote matches concurrent requests with their responses by packet ID:

- Each {meth}`~rmote.protocol.Protocol.__call__` invocation generates a unique `packet_id`
  from a thread-safe counter.
- An `asyncio.Future` is stored in `Protocol.futures` keyed by `packet_id`.
- The `_loop` task continuously reads incoming packets. When a response arrives its
  `packet_id` is used to look up and resolve the corresponding future.
- Remote-side handlers run as asyncio tasks. A `def` Tool method runs in a worker
  thread; an `async def` method runs in the remote loop. Blocking work inside an
  async method blocks that loop. See {doc}`writing-tools` for method selection.
