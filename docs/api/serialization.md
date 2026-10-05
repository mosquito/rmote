# Dataclass serialization

These helpers are independent of facts and collector modules. Use
`json.dumps(value, default=json_default)` and
`json.loads(payload, object_hook=json_object_hook)` to preserve dataclasses and
bytes alongside ordinary JSON values. The default hook uses `singledispatch`;
the object hook handles tagged records through pattern matching.

Bytes use `{"_type": "bytes", "_value": "AP8="}` (base64). Dataclasses use
`{"_type": "dataclass", "_value": {"class": "module:qualname", "fields": {...}}}`.
Fields are read shallowly; json handles nested values without an `asdict` or
`deepcopy` pass. Constructor fields are saved, and `init=False` fields are
recomputed by the restored model.

The exact two-key `_type`/`_value` shape is reserved, including in raw data.
Unknown or malformed tagged records raise an error. Other dictionary shapes
are unchanged. Decode only trusted data: restoring a dataclass imports its
class and calls its constructor. JSON's usual rules apply to native containers,
including tuples becoming lists and supported non-string keys becoming strings.
Sets, arbitrary objects and immutable mapping wrappers are unsupported unless
the caller supplies an appropriate serializer.

```{eval-rst}
.. automodule:: rmote.serialization
   :members: Dataclass, json_default, json_object_hook, dump_dataclass, load_dataclass
```
