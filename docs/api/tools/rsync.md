# Rsync

Use `Rsync.upload` and `Rsync.download` to add directory synchronization to
Python automation over an existing rmote connection. This lets file transfers
share the same connection as your other remote tools. For synchronization
from a terminal, {doc}`rmote rsync <../../rsync>` manages that connection and
adds transfer logs and a summary around these APIs.

The tool uses rmote's common {doc}`transports <../../transports>`: SSH,
Docker/Kubernetes exec, local Python and prepared byte streams. The transport
reaches Python; rmote supplies the synchronization code.

```{eval-rst}
.. autoclass:: rmote.tools.rsync.Rsync
   :members: upload, download
```

## Strict receive decoding

Rsync uses FileSync's `Batch` and `SyncResult`, and its own `Entry` directory
model in remote calls. An explicit `pickle_allowlist` must permit the types used by that exchange;
permitting only the local `Result` is insufficient. Some files can already be
installed when a reply is refused. See {doc}`file_sync` and {doc}`../protocol`
for the strict decoding contract. The default examples use ordinary decoding.

## Types

```{eval-rst}
.. autoclass:: rmote.tools.rsync.Result
   :exclude-members: changed, files, transferred, reused, directories, symlinks, deleted
```
