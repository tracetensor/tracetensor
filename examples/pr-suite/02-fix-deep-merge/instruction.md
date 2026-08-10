# Fix deep_merge — nested config patch broken

## PR description

`deep_merge(base, patch)` in `/app/merge.py` should recursively merge two dicts.
When both sides have a **dict** at the same key, merge recursively — do not
replace the whole subtree.

Fix `/app/merge.py`. Do not change tests.

### Examples

```python
deep_merge({"a": {"x": 1}}, {"a": {"y": 2}})
# → {"a": {"x": 1, "y": 2}}

deep_merge({"a": 1}, {"b": 2})
# → {"a": 1, "b": 2}
```

Non-dict values in `patch` overwrite `base` as usual. Neither input is mutated.
