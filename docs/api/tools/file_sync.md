# FileSync

FileSync uses the same {doc}`transports <../../transports>` as other rmote tools,
including SSH, Docker/Kubernetes exec, local Python and prepared byte streams.

```{eval-rst}
.. autoclass:: rmote.tools.file_sync.FileSync
   :members: upload, download
```

## Types

```{eval-rst}
.. autoclass:: rmote.tools.file_sync.SyncResult
   :exclude-members: changed, size, transferred, reused
```

## What a transfer costs

Measured by `python -m benchmarks.file_sync` on one quiet machine, with a local
subprocess as the target and a file of 128 MiB of random bytes. Every call
through the protocol is counted, so the calls are the round trips the exchange
needs. A link with a long round trip pays that time once per call.

| block | first transfer | calls | repeat | calls |
| ---: | ---: | ---: | ---: | ---: |
| 1 MiB | 610 MiB/s | 18 | 1 332 MiB/s | 5 |
| 4 MiB | 605 MiB/s | 18 | 1 327 MiB/s | 4 |
| 8 MiB | 601 MiB/s | 18 | 1 292 MiB/s | 4 |
| 16 MiB | 482 MiB/s | 10 | 1 233 MiB/s | 4 |

One block per call cost 258, 66, 34 and 18 calls for the same four rows, and
the alternating comparison at the default block gives 614 against 585 MiB/s on
the first transfer and 1 320 against 1 190 MiB/s on the repeat. The block size
no longer decides the number of round trips, so the default of 4 MiB fits a
link with a long round trip as well.

## Low-level API

```{eval-rst}
.. autoclass:: rmote.tools.file_sync.Session
   :members: check, step, send, receive, decide, keep, append, start, finish, close
```

```{eval-rst}
.. autoclass:: rmote.tools.file_sync.Batch
   :exclude-members: signatures, blocks, digest
```
