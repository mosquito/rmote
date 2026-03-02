# Concepts

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

1. **Definition** - {class}`~rmote.protocol.ToolMeta` metaclass captures the class source via
   `inspect.getsource` at class-definition time.

2. **Serialization** - {func}`~rmote.protocol.tool_to_dict` packages the source into a dict,
   stripping local `rmote.*` imports that are irrelevant on the remote side.

3. **Transfer** - A `SYNC | REQUEST` packet carries the dict to the remote side.

4. **Reconstruction** - {func}`~rmote.protocol.tool_from_dict` runs `exec` on the source
   inside a fresh module namespace, then caches the resulting class in `sys.modules`.

5. **Registration** - On the remote side, the tool instance is stored in `Protocol.tools` keyed
   by its **module-qualified name** (`module.ClassName`). Inline tools (defined inside a function)
   use the bare class name. This means two Tool subclasses with the same class name in different
   modules are dispatched correctly — they occupy separate keys.

6. **Cache** - The local `Protocol` instance maintains a `_tools_cache` set of already-synced
   tool classes. Before every RPC call, `__call__` checks whether the tool class is in this set.
   If it is not, steps 2–5 run and the class is added to the set; if it is, the SYNC is skipped
   entirely and only the RPC packet is sent.

**Lazy** - No SYNC packets are sent when a connection is opened or when a Tool class is defined.
The transfer happens the first time a method on that class is actually *called* through a given
connection.

**Per-connection** - The cache lives on the `Protocol` instance. Each new connection starts with
an empty cache, so a tool that was synced on a previous connection will be re-synced on first use
through the new one.

## Concurrency Model

rmote uses asyncio on both sides to handle multiple in-flight requests without blocking:

- Each {meth}`~rmote.protocol.Protocol.__call__` invocation generates a unique `packet_id`
  from a thread-safe counter.
- A `asyncio.Future` is stored in `Protocol.futures` keyed by `packet_id`.
- The `_loop` task continuously reads incoming packets. When a response arrives its
  `packet_id` is used to look up and resolve the corresponding future.
- Remote-side handlers are wrapped in `asyncio.create_task`, so many RPC calls can
  execute concurrently even if some block on I/O.

```{mermaid}
sequenceDiagram
    participant C as caller (your code)
    participant L as Local _loop
    participant R as Remote _loop

    C->>L: await proto(Tool.method, *args)
    Note over L: packet_id = get_id()<br/>futures[id] = Future()
    L->>R: REQUEST {method, args, id=N}
    activate R
    Note over R: create_task(_handle_rpc_request)

    C->>L: await proto(Tool.other, *args)
    Note over L: packet_id = get_id()<br/>futures[id] = Future()
    L->>R: REQUEST {other,  args, id=N+1}

    R-->>L: RESPONSE {result_1, id=N}
    deactivate R
    Note over L: futures[N].set_result(result_1)
    L-->>C: result_1

    R-->>L: RESPONSE {result_2, id=N+1}
    Note over L: futures[N+1].set_result(result_2)
    L-->>C: result_2
```
