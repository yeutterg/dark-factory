# Vendored Warp Factories test data

Test data only; no runtime code reads it.

- `examples/`: copied from https://github.com/warpdotdev/warp-factory-examples at
  commit `76f0f06f59a3c3d5fc29eb7699fe281073fa58c1` (MIT, see `LICENSE`).
- `schemas/`: the published `v1alpha1` JSON Schemas from
  `https://app.warp.dev/api/v1/factory-files/schemas/v1alpha1/<name>.schema.json`,
  retrieved 2026-11-26.

`tests/test_warp_conformance.py` checks that every example tree the schemas accept is
also accepted by Dark Factory's hand-written reader, and that the reader rejects what
the schemas reject. Refresh both together and review the diff.
