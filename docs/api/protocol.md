# Protocol and Tool Support

## Asynchronous connections

Use `Protocol` from an asynchronous application. Enter its async context
before calling tools. See {doc}`../quickstart` for a complete connection example
and {doc}`../concepts` for subprocess ownership and cancellation.

The {doc}`transport guide <../transports>` explains command prefixes, local
Python and prepared streams such as `nc`, including when to use
`from_command` or `from_subprocess`.

```{eval-rst}
.. autoclass:: rmote.protocol.Protocol
   :members:
   :show-inheritance:
   :special-members: __call__, __aenter__, __aexit__
```

## Tool definitions and commands

Subclass `Tool` in a readable Python module. Import `async_process` from
`rmote.process` and use `await async_process(...)` inside async tool methods to
run commands without blocking other calls; set `capture_output=True` when the return value needs output.
Built-in command tools use async methods, so direct local calls require
`await`. Calls through `Protocol` and the synchronous `Connection` keep their
normal calling conventions. The synchronous `process` helper remains available.
See {doc}`process` for subprocess helpers and {doc}`../writing-tools` for executable examples.

```{eval-rst}
.. autoclass:: rmote.protocol.Tool
   :members:
   :show-inheritance:

.. autofunction:: rmote.protocol.inline
```

## Streaming calls

A tool method written as an async generator streams its result. The items
travel as responses with the `STREAM` flag set, and the ordinary response that
follows them ends the stream. The remote side pushes the items as it produces
them and does not wait for a request per item. The items that it has ready
travel together in one packet, which the `BATCH` flag marks.

The call is native. `protocol(Tool.method, ...)` gives a coroutine for an
ordinary method and an async iterator for an async generator:

```python
async for item in protocol(Tool.items):
    handle(item)
```

Annotate the method as returning `AsyncIterator[T]`, or the type checker reads
the call as an ordinary one.

Frame fragmentation keeps a long stream from holding the channel, so ordinary
calls keep their progress while a stream runs.

Flow control is a permission budget. The sender may have `MAX_STREAM_WINDOW`
serialized bytes and `MAX_STREAM_BACKLOG` items in flight, and the consumer
gives both back in batches when it asks for the next item. A slow consumer
therefore slows the sender instead of filling memory. The count limit goes with
the byte budget, so a long run of small items cannot pass it.

The budget counts the serialized size of an item before compression. That is
not the memory of the Python object, which can be larger or smaller, but it is
what the peer has to hold and what the wire carries.

An item above `MAX_STREAM_ITEM` is refused with a `ValueError`, because it could
never find room. An item that is merely larger than the whole budget still goes
out, alone, once nothing else is in flight.

The limits are enforced again on the receive side, as the last defence against a
peer that ignores the budget. Such a peer ends the stream with a `RuntimeError`.

### How many streams one connection carries

The items that a sender has ready travel in one packet, so a write on one side
and a read on the other serve a burst instead of serving every item. The budget
of the stream bounds that packet, so one of them holds at most 256 items or
4 MiB. Measured on a local transport with 64-byte items, 16 384 of them in
total, by `python -m benchmarks.streams`, with both ways in alternating runs:

| streams | µs per item | before | reads per item | before | local CPU µs | before |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 3.01 | 7.26 | 0.00 | 0.01 | 0.61 | 6.71 |
| 4 | 2.03 | 6.82 | 0.00 | 0.00 | 0.61 | 6.71 |
| 8 | 2.08 | 16.31 | 0.00 | 0.69 | 0.61 | 12.21 |
| 16 | 2.11 | 15.63 | 0.00 | 0.63 | 0.61 | 11.60 |
| 32 | 2.06 | 16.26 | 0.00 | 0.71 | 0.61 | 12.21 |

The "before" columns are the measurement of one packet per item. A connection
then carried a handful of streams at full speed and twice the cost beyond it,
because no stream filled its window any more and every item paid a syscall on
both sides. Nothing of that is left: the cost per item no longer depends on how
many streams share the connection.

Equal streams still do not finish together. The last of thirty-two finishes
about 40 percent of the run after the first, against 33 percent with one packet
per item, because a stream that is served sends a whole batch. The run itself
is eight times shorter, so the difference between the first and the last finish
fell from 87 ms to 15 ms. A smaller batch makes the finishes closer and the run
longer: a limit of eight items per packet gives a spread of 17 percent and
3.66 µs per item. Where every stream must advance evenly, give each one its own
connection.

### When a stream is the right shape

The same ten thousand small items, over one local connection, in three shapes:

| shape | total | per item |
| --- | ---: | ---: |
| one call returning a list | 4.5 ms | 0.45 µs |
| a stream | 91.1 ms | 9.11 µs |
| one call per item | 1 994 ms | 199 µs |

A call per item is never the answer: a round trip costs about 200 µs whatever
it carries. One call with a list is the cheapest shape while the result fits in
memory on both sides and the caller needs all of it: it serializes and
compresses once. A stream earns its keep when the result must not be held
whole, when the items appear over time, or when the consumer has to see the
first ones before the last ones exist. The gap narrows as items grow, because
the per-item cost stops dominating.

### What a stream does not break

These properties were measured and need no change.

An ordinary call keeps its progress while a stream of 1 MiB items runs, because
every packet travels as 64 KiB frames and the write lock is released between
them. Latency of a call that returns at once, 60 samples on a free channel and
during such a stream:

| channel | minimum | p50 | p95 | maximum |
| --- | ---: | ---: | ---: | ---: |
| free | 0.181 ms | 0.189 ms | 0.215 ms | 0.236 ms |
| a stream of 1 MiB items | 0.184 ms | 0.203 ms | 0.310 ms | 0.449 ms |

A slow consumer costs the producer nothing beyond its own pace, and neither
side spins while it waits. Two hundred items of 32 KiB with a pause per item:

| pause per item | wall | the pauses alone | local CPU | remote CPU |
| ---: | ---: | ---: | ---: | ---: |
| none | 0.006 s | 0 s | 0.010 s | 0.006 s |
| 1 ms | 0.256 s | 0.200 s | 0.000 s | 0.008 s |
| 5 ms | 1.211 s | 1.000 s | 0.040 s | 0.031 s |

The consumer returns permission in batches of half the budget, which a sweep of
batch sizes showed to be on a plateau: returning it per item costs twice as
much per item, a batch of 64 is within noise of the current one, and a batch of
255 loses 11 percent because it nearly equals the window of 256 items, so the
sender runs out and waits. That sweep ran under load from other work on the
machine, so its ratios are the reliable part. Do not change the rule.

To leave the loop tells the sender to stop, so an abandoned stream frees the
peer. A bare `break` does not close the iterator at once, so use
`contextlib.aclosing` when the close has to be immediate:

```python
async with contextlib.aclosing(protocol(Tool.items)) as items:
    async for item in items:
        if enough(item):
            break
```

Closing the iterator sends a stop notification. That notification releases the
sender's permission and cancels the task that produces the items. The remote
generator therefore receives the cancellation wherever it waits, its `finally`
runs, and the task leaves the peer. A generator that waits for data it never
receives ends the same way as one that keeps producing.

Two limits remain. Synchronous code inside the method cannot be interrupted:
the cancellation arrives at the next `await`, so a method that blocks a thread
keeps blocking it. And a resource that the method holds between calls needs its
own call to release it, exactly as with an ordinary call:

```python
key = await protocol(Vty.open, "/bin/sh")
async with contextlib.aclosing(protocol(Vty.output, key)) as output:
    async for chunk in output:
        if enough(chunk):
            break
# The session waits for more output, so a separate call releases it.
await protocol(Vty.close, key)
```

## What the wire layer costs

Measured on a local transport with both sides on one quiet machine, best of 9,
by `python -m benchmarks.wire`. The round trip of a call that returns its own
argument, with the part this side spends before the bytes leave:

| payload | round trip | serialize | send | everything else |
| ---: | ---: | ---: | ---: | ---: |
| 8 B | 167 µs | 3.0 µs | 3.1 µs | 161 µs |
| 100 B | 159 µs | 2.3 | 2.6 | 154 |
| 1 KiB | 208 µs | 3.6 | 13.9 | 190 |
| 16 KiB | 244 µs | 3.6 | 24.7 | 216 |
| 256 KiB | 742 µs | 12.0 | 230 | 501 |
| 1 MiB | 2 167 µs | 38.5 | 803 | 1 326 |

The floor is about 160 µs, of which this side spends 6: the rest is two passes
through a pipe and the whole remote side, including the worker thread that a
synchronous Tool method runs in. A method marked with
{func}`rmote.protocol.inline` skips that thread.

One connection serves many calls at once, because a call waits for bytes rather
than for the processor:

| calls in flight | per call | calls per second |
| ---: | ---: | ---: |
| 1 | 233 µs | 4 300 |
| 8 | 69 µs | 14 400 |
| 64 | 42 µs | 23 900 |

Bytes per second for content that compresses and for content that does not. The
sender refuses to compress dense data, so random bytes are faster than text:

| payload | content | upload | download |
| ---: | --- | ---: | ---: |
| 1 MiB | compressible | 845 MiB/s | 871 MiB/s |
| 1 MiB | random | 1 150 MiB/s | 1 162 MiB/s |
| 8 MiB | compressible | 1 001 MiB/s | 981 MiB/s |
| 8 MiB | random | 1 385 MiB/s | 1 284 MiB/s |

Two parts of the wire layer were measured and are not worth tuning. The packet
header costs 43 ns to pack and 33 ns to unpack, which is 0.06 percent of the
round trip of a small call. Slicing a payload into frames costs 1.5 µs per
frame, and the join on the other side costs less than that.

### What a remote log record costs

A record that a Tool method writes reaches the local handlers of the caller. It
travels inside the response of that call, so it needs no packet of its own.
Measured by `python -m benchmarks.logs` on the same quiet machine, with one
record per call and a local handler that keeps nothing. Both ways share one
process and the rounds alternate, so the load of the machine hits them equally:

| threads | the record travels | calls per second | share of the run without records | loop CPU per record | records with a packet |
| ---: | --- | ---: | ---: | ---: | ---: |
| 8 | with the response | 11 982 | 0.80 | 9.1 µs | 4% |
| 8 | in its own packet | 11 444 | 0.77 | 14.1 µs | 70% |
| 8 | filtered on arrival | 12 873 | 0.86 | 4.8 µs | 3% |
| 32 | with the response | 17 723 | 0.86 | 3.5 µs | 3% |
| 32 | in its own packet | 17 666 | 0.86 | 4.0 µs | 19% |
| 32 | filtered on arrival | 18 484 | 0.90 | 3.0 µs | 3% |

A record keeps a packet of its own when it cannot wait for the response: when
another batch is on the wire, or when the batch is above the limits below.

The level of the local logger is tested where the packet arrives. A record that
no local handler accepts is dropped there, so it costs no hand-off to the
delivery thread and no `logging.LogRecord`: raise the level of
`rmote.remote.<name>` for the records you do not want, and they become almost
free. That price, 0.86 of the run at eight threads, is also what the wire alone
costs: with the delivery replaced by an empty function the share is the same.
A record a handler does want therefore costs 0.06 of the run more, which is the
hand-off and the work of the handlers; they run in the delivery thread and
compete with the loop thread for the interpreter.

A burst of more than `RemoteLogHandler.ATTACH_RECORDS` records, or one above
`ATTACH_BYTES` of text, keeps a packet of its own, so a response never becomes
a fragmented packet because of the records it carries. A record of a call that
runs long waits no more than `ATTACH_DELAY` for a response and then travels
alone. Records of one logger always arrive in the order they were written.

## Frame fragmentation

A logical packet travels as one or more frames. Every frame repeats the logical
flags of the packet. The `FRAGMENT` flag marks each frame except the last one,
so the receiver knows that more data follows.

`BaseProtocol.FRAGMENT_SIZE` sets the largest payload in one frame. The sender
serializes the whole packet first, then writes the fragments and compresses each
body as it goes. It releases the write lock between frames. Packets with a
different `packet_id` therefore interleave on the wire, and one large transfer
cannot hold the channel for its whole duration. A frame that arrives above
`FRAGMENT_SIZE`, before or after inflation, is refused: no sender of this
protocol writes one.

The receiver keeps one buffer per `packet_id` in a {class}`rmote.protocol.FragmentBuffer`.
A packet that completes first is returned first, in any fragment order. The
buffer enforces `MAX_REASSEMBLY_BYTES` and `MAX_PARTIAL_PACKETS` and raises
`ValueError` when a peer passes a limit.

Fragments of one `packet_id` must come from one task. The receiver joins them in
arrival order, so concurrent senders on the same id would mix their bytes.

(frame-compression)=
## Frame compression

Each frame body is compressed on its own, against the history of the
connection. One `zlib` dictionary serves each direction, at
`BaseProtocol.COMPRESSION_LEVEL`, and every compressed body ends with a sync
flush. A body that repeats the words of an earlier one therefore carries a
reference instead of the words, which is what makes a small packet cheap: the
method name, the flags and the shape of a result are the same in every call.

`COMPRESSED` marks the bodies that went through the dictionary. The receiver
inflates exactly those, in the order they arrive, before the fragments of a
packet are joined. The bootstrap and the ready boundary travel plain, and the
boundary opens the codec of each direction.

A body whose first `DENSITY_SAMPLE` bytes use more than `DENSITY_LIMIT` byte
values travels raw and leaves the dictionary untouched, so random and already
compressed data costs one short sample. No trial compression runs, because a
copy of the dictionary costs more than the pass it would test: the deflate
state holds its whole window. A body that passes the sample and still does not
shrink therefore travels compressed and grows by the few bytes of its deflate
block.

A tool that knows its data cannot shrink says so, and the call carries that
decision to the request, the response and the items of a stream:

```python
value = await remote.uncompressed(Tool.method, *args, **kwargs)
async for item in remote.uncompressed(Tool.items):
    handle(item)
```

`FileSync` decides from the name and the size of the file, so an archive or an
image is not compressed twice while text and JSON still are.

Measured on a local transport by `python -m benchmarks.wire` and a byte count
of the same calls, against the policy that compressed each packet on its own,
in alternating runs:

| workload | packet policy | frame codec |
| --- | ---: | ---: |
| 100 empty calls | 201–203 µs, 10 700 B | 210 µs, 3 000 B |
| 100 calls of 1 KiB JSON | 225–227 µs, 20 300 B | 210–213 µs, 4 177 B |
| 5 echoes of 1 MiB of text | 2.60–2.72 ms, 34 000 B | 2.62–2.64 ms, 37 938 B |
| 5 echoes of 1 MiB of random | 1.89–1.93 ms, 5 245 220 B | 1.91–1.95 ms, 5 244 800 B |
| calls per second, 1 in flight | 3 956–4 107 | 3 864–3 922 |
| calls per second, 8 in flight | 17 093–17 293 | 15 038–16 178 |
| calls per second, 64 in flight | 30 985–31 260 | 27 047 |
| stream items, 8 streams | 1.71–1.75 µs | 1.77 µs |

Small traffic costs three to five times fewer bytes, because the dictionary
answers for what repeats. The price is the deflate pass of every frame: one
call in flight loses 3 percent of the calls per second, eight lose 8 percent
and sixty-four lose 13 percent. Bulk transfers and streams keep their speed,
and bulk text costs 12 percent more bytes, because the codec flushes every
frame while one gzip pass had the whole packet. Random data is unchanged: both
policies refuse to compress it.

A local transport carries bytes for free, so these numbers are the cost
without the gain. The gain appears where the link is slower than the
processor; that case is not measured here.

A connection that needs the old policy sets `FRAME_CODEC` to False on its
protocol class. Both sides run the same source, so they always agree; a peer
that was started separately must use the same setting.

A compressed body that does not end at a flush boundary, inflates above
`FRAGMENT_SIZE`, or carries bytes after the end of its stream ends the
connection with a `ValueError`. The dictionary of that direction cannot be
repaired, because it no longer matches the sender.

## Protocol internals

The clients use these helpers for bootstrap, source transfer, and packet
handling. Application code normally calls tools through a connection.

```{eval-rst}
.. autoclass:: rmote.protocol.BaseProtocol
   :members:
   :show-inheritance:
```

```{eval-rst}
.. autoclass:: rmote.protocol.Flags
   :members:
   :show-inheritance:
```

```{eval-rst}
.. autoclass:: rmote.protocol.FragmentBuffer
   :members:
   :show-inheritance:
```

```{eval-rst}
.. autoclass:: rmote.protocol.Packet
   :members:
   :show-inheritance:
```

```{eval-rst}
.. autoclass:: rmote.protocol.StreamCredit
   :members:
   :show-inheritance:
```

```{eval-rst}
.. autofunction:: rmote.protocol.streaming_method
```

```{eval-rst}
.. autofunction:: rmote.protocol.tool_to_dict
```

```{eval-rst}
.. autofunction:: rmote.protocol.tool_key
```

```{eval-rst}
.. autofunction:: rmote.protocol.tool_from_dict
```

```{eval-rst}
.. autofunction:: rmote.protocol.bootstrap_packer
```

```{eval-rst}
.. autoclass:: rmote.protocol.ModuleBundle
   :members:

.. autoclass:: rmote.protocol.ModuleLoader
   :members:
```

```{eval-rst}
.. autoclass:: rmote.protocol.ModulePickler
   :members:
```
