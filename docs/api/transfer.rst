File synchronization
====================

``upload`` and ``download`` accept a synchronous ``Connection``. Paths are interpreted in
transfer order: source first, destination second. For upload the source is local;
for download the source is remote.

.. code-block:: python

   from rmote.sync import Connection
   from rmote.transfer import download, upload

   with Connection.from_ssh("deploy@example.org") as connection:
       result = upload(connection, "./app.tar", "/srv/app.tar")
       print(result.changed, result.transferred, result.reused)
       download(connection, "/var/log/app.log", "./app.log")

Async applications use ``async_upload`` and ``async_download`` with an open ``Protocol``:

.. code-block:: python

   from rmote.transfer import async_download, async_upload

   result = await async_upload(protocol, "./app.tar", "/srv/app.tar")
   await async_download(protocol, "/var/log/app.log", "./app.log")

How it works
------------

The sender reads a block and sends its length and SHA-256. The receiver reads the
corresponding block and compares the signature. Only a mismatch causes the sender
to transmit block contents. Both directions use the same algorithm and endpoint
code; no complete hash manifest or file contents are retained in memory.

Blocks default to 1 MiB (configurable from 1 byte to 16 MiB). This initial
implementation negotiates one block at a time, so latency limits throughput.
Insertions can shift subsequent block boundaries and cause large retransfers.
``transferred`` counts file payload bytes, excluding RPC framing, hashes and
compression. ``reused`` counts bytes copied from the existing destination.

The receiver assembles a temporary file in the destination directory, verifies
the cumulative SHA-256, flushes and fsyncs the file, and atomically replaces the
destination. Growth, truncation and empty files are supported. Identical files
retain their inode and timestamps, although comparison still reads both files
and writes a temporary copy. Changed files require space for a complete new copy.

Scope and failures
------------------

* Regular files only; symlinks and special files are rejected. Parent directories
  must exist. Directory synchronization and resuming interrupted transfers are
  not implemented.
* Existing destination ownership and permission bits are retained; new files are
  created with mode 0600. Source metadata, ACLs and extended attributes are not
  synchronized. Atomic replacement does not update other hard links.
* Keep both files stable during transfer. Size, inode and timestamp checks detect
  ordinary concurrent modifications but are not locks or filesystem snapshots.
* Errors before replacement leave the destination untouched. Cancellation waits
  for the current RPC, then closes sessions before the next operation. If the
  final replacement is already in flight, cancellation cannot undo it.
* A failed connection can prevent remote cleanup; abrupt process termination can
  leave a ``.<filename>.rmote-*`` temporary file. The original destination remains
  intact unless replacement already completed. After an ambiguous final RPC
  failure, rerun synchronization to establish the resulting state.
* Synchronous RPC deadlines do not stop remote work. Prefer a connection without
  per-RPC deadlines during transfer; cancelling the async helper waits for the
  in-flight operation and can therefore wait on a stalled connection.

API
---

.. automodule:: rmote.transfer
   :members: upload, download, async_upload, async_download

.. autoclass:: rmote.tools.file_sync.SyncResult
   :members:
