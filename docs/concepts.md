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
| Subprocess | Owned by the connection | `from_command` and `from_ssh` own the transport process; `from_subprocess` leaves ownership with its caller |
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

Both clients preserve a Tool method's argument and result types. For async
generator methods, use `Protocol`: its call and `stream()` return an async
generator with the method's item type. `Connection` rejects these methods with
`TypeError` before sending an RPC.

## Cancellation and Logging

Synchronous RPC deadlines include first Tool upload, packet transmission, and
response waiting. Timeout raises `TimeoutError`; `KeyboardInterrupt` propagates.
Both cancel local waiting and remove the pending request. Async callers can use
`asyncio.timeout` around an operation to request the same local cancellation.

Cancelling an ordinary RPC stops local waiting without sending a remote
cancellation packet. Remote work and its side effects can continue. Late
responses for cancelled or unknown request IDs are ignored.

Streaming calls have a separate stop notification. Closing the local iterator
with `aclose()` or `contextlib.aclosing` sends it while the connection remains
open. The sender stops at its next permission check and closes the remote
generator then. The notification does not interrupt an `await` inside the
generator; a method waiting for data may need a separate resource-close call.
A bare `break` does not close an async iterator immediately. See the
{doc}`streaming lifecycle <api/protocol>` for examples.

Cancellation while sending a packet can leave an incomplete frame and close
the channel. A transport failure requires closing the connection and creating
another; automatic reconnect is not available.

The close deadline limits graceful process shutdown. Cleanup then uses terminate,
a one-second grace period, and kill. Total cleanup can take longer because
local cancellation and executor shutdown require cooperative code.

Remote log records invoke local logging handlers in a delivery thread of their
own, not in the loop thread. A handler can therefore take as long as it needs
without delaying the calls of the connection. Keep handlers thread safe. A
handler must not call synchronous methods of that connection, because the close
waits for the records that wait. The queue is bounded: when handlers cannot keep
up, the records that do not fit are counted and the loss is reported through
the `rmote.remote` logger. A record that the level of its local logger excludes
is dropped where the packet arrives, so it never reaches that thread. The loop
remains active between RPCs, so background events can arrive while the caller
is idle.

The records that a call writes travel inside the response of that call, because
that packet leaves anyway. A record of a call then costs no packet of its own.
A record that appears between calls, or one of a call that runs long, travels
in a packet of its own within a millisecond. Records of one logger arrive in the
order they were written, whichever way they travelled. See
{doc}`what a record costs <api/protocol>`.

Use `Protocol` in async applications. Alternatively, use `asyncio.to_thread`
for the complete synchronous lifecycle. A direct synchronous call blocks the
calling loop. Cancelling a `to_thread` await does not stop its worker thread.

## Bootstrap Flow

The chosen {doc}`transport <transports>` connects the client to a Python
interpreter's stdin/stdout stream. For example, an SSH connection starts
`ssh -T host python3 -qui`; Docker/Kubernetes exec and local subprocesses carry
the same bootstrap. The target needs a compatible Python interpreter;
installing rmote or copying the tool project there is not required.

When {meth}`~rmote.protocol.BaseProtocol.from_subprocess` is called, rmote creates
the `rmote` package and injects `rmote.protocol` into the remote Python
interpreter through stdin as a gzip-compressed, base64-encoded payload. Tool
and template modules arrive later, when needed:

```text
exec(decompress(b64decode("...")))   # creates rmote and loads rmote.protocol
asyncio.run(_protocol.run())         # starts the remote event loop
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

### Frames and logical packets

A logical packet carries one request, response, log record or stream-control
message. A response that carries the log records of its call sets `LOG` as
well, and the records are then delivered before the result. It travels as one or more frames. Each frame has a fixed-size 21-byte
header (struct `">5sIIQ"`) followed by that frame's payload bytes:

```{mermaid}
packet-beta
  0-39:   "magic - b'RMOTE' (5 bytes)"
  40-71:  "flags - Flags IntFlag (4 bytes)"
  72-103: "length - frame payload bytes uint32 (4 bytes)"
  104-167: "packet_id - request correlator uint64 (8 bytes)"
```

The sender pickles the object first. When new module dependencies are needed,
it wraps the source manifest and those object-pickle bytes in an outer pickle
and sets `MODULES`.

The resulting bytes are split into frames of at most `FRAGMENT_SIZE` (64 KiB
by default). All frames repeat the logical packet's flags and `packet_id`;
`FRAGMENT` is added to every frame except the last. A frame may contain only
part of a pickle and cannot be decoded on its own.

Each frame body is then compressed on its own, against the history of the
connection: one zlib dictionary serves each direction, and every compressed
body ends with a sync flush. A body that repeats the words of an earlier one
therefore carries a reference instead of the words, which is what makes a
small packet cheap. A body that does not shrink travels raw and leaves the
dictionary untouched, so random or already compressed data costs one trial.
`COMPRESSED` marks the bodies that went through the dictionary, and the
receiver inflates exactly those, in the order they arrive.

The receiver reassembles the inflated frames by `packet_id`, installs any
source manifest, then unpickles the object. Frames from different packet IDs
can interleave because the sender releases the write lock between frames.
Frames for the same ID must be sent in order by one task. See
{doc}`api/protocol` for the codec, reassembly limits and streaming budgets.

Bootstrap and the ready boundary travel plain, and the bootstrap carries the
current protocol implementation to the remote interpreter. Both sides
therefore run the same codec. Independently started peers must agree on it:
a frame dictionary cannot be read by a peer that expects a gzip packet.

### Flags

The `flags` field is a combination of {class}`~rmote.protocol.Flags` values:

| Flag         | Value | Meaning                              |
|--------------|-------|--------------------------------------|
| `COMPRESSED` | 1     | This frame body went through the zlib dictionary of its direction |
| `REQUEST`    | 2     | Request or stream-control direction  |
| `RESPONSE`   | 4     | This is a response to a request      |
| `SYNC`       | 8     | Tool synchronization                 |
| `RPC`        | 16    | Remote procedure call                |
| `EXCEPTION`  | 32    | Response carries an exception        |
| `LOG`        | 64    | Log records forwarded from remote; with `RESPONSE`, the payload is `(records, result)` |
| `FRAGMENT`   | 128   | More frames of this packet follow    |
| `STREAM`     | 256   | Stream item or permission control    |
| `MODULES`    | 512   | Source manifest before object pickle |
| `BATCH`      | 1024  | Payload holds several items of one stream, each behind its uint32 length |

An ordinary `SYNC | REQUEST` or `RPC | REQUEST` expects a response. Streaming
methods start with the same `RPC | REQUEST`; their items arrive as
`RPC | RESPONSE | STREAM | BATCH`, where one packet carries the items the
sender had ready, followed by an ordinary response that ends the stream. An
exception response reports a failure after preceding items.

The consumer returns permission with `RPC | STREAM | REQUEST` on the stream's
original packet ID. Its payload is `(serialized_bytes, item_count)` to return
consumed credit, or `None` to stop the sender. These control messages do not
receive acknowledgements. `REQUEST` and `RESPONSE` distinguish the producing
and consuming sides of a stream.

## Minimal bootstrap and lazy dependencies

Bootstrap creates the `rmote` package and loads `rmote.protocol`, using only
Python's standard library. Template engines, filters and tools are transferred
on demand. Import `Template` from `rmote.templates`; neither the root package
nor `rmote.protocol` re-exports it.

Tool definitions carry their declared module dependencies. Object arguments and
results can carry an outer source manifest with the `MODULES` flag. The receiver
registers these sources before unpickling the enclosed object bytes, allowing
Python to import their classes normally. This also handles templates nested in
containers, custom filter reducers and streaming results. Successful transfers
are remembered per connection. There is no missing-module round trip during
unpickling and no topological execution schedule.

## Tool Serialization Lifecycle

For example, `await remote(Disk.free_bytes, "/")` sends the code needed to
define `Disk` before requesting `free_bytes`. The method body runs in the
remote interpreter; the local caller receives its return value or exception.

Tools are transferred lazily and cached for the lifetime of the connection:

1. `ToolMeta` records class source for inline tools. File-based classes use
   the same cached `ModuleBundle` as module functions, preserving their imports.
2. The first call sends the bundle in a `SYNC | REQUEST` packet. The receiver
   registers its sources with `ModuleLoader` and imports the defining module.
   Inline classes execute their class source in a separate namespace.
   The sources of a package travel once for a connection. Every class of the
   package shares one bundle, so a class announced later carries only the
   modules the peer still lacks, which is usually none of them.
3. The receiver constructs a tool instance and stores it in `Protocol.tools`.
   File-based class tools use `module.ClassName` as their key. Inline tools use the bare
   class name, so two inline tools with the same name can collide.
4. Concurrent first calls share loading locks. Classes of one package wait for
   the same lock, so its bundle is built and sent once instead of once for
   every class. Classes from one module reuse that module's namespace. Later
   calls send an RPC packet without another successful upload.

Module functions use the same source bundles and loader. Package bundles
include nested Python modules; the in-memory import loader registers all
sources before importing the defining module. The module itself is stored under
its qualified name in `Protocol.tools`. Python resolves import order, relative
imports and cycles. An explicit `__tool_package__` boundary can include sibling
modules; dependencies outside it must already be available on the target.

The source is compiled and executed in memory. rmote does not install a package
or write the tool's `.py` file into the remote filesystem. The transferred local
filename identifies the code in tracebacks; it is not a deployed file path.
The subsequent RPC contains the method name, arguments and keyword arguments;
arguments, return values and exceptions are serialized with pickle.

Opening a connection does not upload tools. Each connection tracks its own
loaded tools and uploads them on first use. Module/package source bundles use
a process-wide LRU cache shared by connections. Restart the controller after
source changes; loaded remote modules are immutable. See {doc}`writing-tools` for source layout,
dependencies, and custom return types.

## Standard Streams and Trust

The remote bootstrap duplicates the descriptors used for protocol traffic and
redirects standard input, output, and error to `/dev/null`. The protocol copies
are not inherited by child commands. Remote `print()` and uncaptured command
output are discarded; use return values or logging to send information back.

Tool source executes on the target, and messages contain pickled Python values.
Use rmote only with trusted peers and code. The packet format does not provide
authentication or encryption; these come from the chosen {doc}`transport <transports>`.

## Concurrency Model

rmote matches concurrent requests with their responses by packet ID:

- Each ordinary RPC generates a `packet_id` from a thread-safe counter. A
  streaming call allocates its ID when the caller starts reading the iterator.
- Ordinary calls store an `asyncio.Future` in `Protocol.futures`; streaming
  calls store a bounded queue in `Protocol.streams`, keyed by `packet_id`.
- The `_loop` task continuously reads incoming packets. Ordinary responses
  resolve the matching future; stream items and completion responses go to
  the matching queue. Permission messages update the sender's stream budget.
- Remote-side handlers run as asyncio tasks. A `def` Tool method runs in a worker
  thread; an `async def` method runs in the remote loop. Blocking work inside an
  async method blocks that loop. See {doc}`writing-tools` for method selection.
