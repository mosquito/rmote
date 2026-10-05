# Immutable snapshots

`rmote.immutable` freezes a data tree into an independent, read-only snapshot.
It has no dependency on collectors or cache backends and transfers as an
ordinary module dependency when used by remote tools.

```python
from copy import deepcopy

from rmote.immutable import freeze

source = {"services": [{"name": "api", "ports": [8080]}]}
snapshot = freeze(source)
source["services"][0]["ports"].append(8081)
assert snapshot["services"][0]["ports"] == (8080,)
assert deepcopy(snapshot) is snapshot
assert freeze(snapshot) is snapshot

updated = snapshot.updated({"revision": 2})
assert updated["services"] is snapshot["services"]
assert "revision" not in snapshot
```

| Input | Snapshot |
| --- | --- |
| Mapping | `DeepMappingProxy` |
| Builtin list or tuple | Tuple of frozen values |
| Builtin set or frozenset | Frozenset of frozen values |
| Dataclass instance | `DeepMappingProxy` of field names to frozen values |
| Builtin str, bytes, int, float, complex, bool, range, None | Original immutable value |

Dataclass conversion is a projection of fields, including `init=False` fields.
It does not retain the model's type, methods, or attribute access. For example,
`snapshot["network"]["interfaces"]` uses mapping access. It is not a transparent
replacement for a `NetworkInfo` instance or a typed `FactsData` snapshot.
`Cache` stores values as supplied; it does not invoke `freeze` automatically.
Call `freeze` explicitly before caching when this representation is wanted.

Unknown objects, iterators and scalar subclasses raise `TypeError`; they are
not silently exposed as mutable leaves. Mapping keys must be builtin immutable
scalars or tuples/frozensets composed of supported immutable keys. Set members
must remain hashable after freezing; a dataclass projected to a mapping cannot
be a set member. Cycles raise `ValueError`. Shared references in one input graph
remain shared in the resulting snapshot.

Initial freezing takes time and memory proportional to the visited input.
`freeze()` reuses an existing `DeepMappingProxy` without traversing it, and both
`copy.copy()` and `copy.deepcopy()` return that snapshot unchanged. Raw tuples
and frozensets still need inspection because they may contain mutable objects.
`updated(mapping)` shallowly copies the root mapping and freezes only replacement
branches. Old snapshots and unchanged nested branches remain valid and shared.

Pickle preserves the read-only representation, but serialization still traverses
the data and a remote process necessarily receives its own objects. JSON encoders
accept a snapshot, because it is a dict subclass. Source data must not be mutated
concurrently during freezing. This is a read-only Python API, not a sandbox
against code deliberately bypassing it through reflection.

`DeepMappingProxy` is a `dict` subclass, so reading one key, testing one key and
copying the whole mapping run at the speed of `dict`. Every method that would
change the content refuses with `TypeError`: `__setitem__`, `__delitem__`,
`update`, `setdefault`, `pop`, `popitem`, `clear`, `|=` and `fromkeys`. `copy()`
gives the snapshot itself, as `copy.copy()` does. The combine `snapshot | other`
gives a new plain dictionary and leaves the snapshot unchanged. A read-only
mapping cannot refuse `dict.__setitem__` called explicitly on it; the contract
is the API, not the storage.

```{eval-rst}
.. autoclass:: rmote.immutable.DeepMappingProxy
   :members: updated

.. autofunction:: rmote.immutable.freeze
```
