# Change: Map on a Python without blake2b

## Why
`leyline map` crashes with `AttributeError: module 'hashlib' has no attribute 'blake2b'` on a CPython built
without its `_blake2` module (a pyenv 3.12 on this machine). The hash is an 8-byte fingerprint of a group's rows;
any digest serves.

## What Changes
- `incremental._fingerprint` gives 8 bytes: blake2b when `hashlib` has it, looked up on each call, else the
  first 8 bytes of sha256.
- The two group hashes in `Run._groups` use it.

## Impact
`code_version` folds `sys.version` into the cache key, so a cache written by one Python build is never read by
another: existing caches stay valid where blake2b exists, a build without it starts its cache fresh, and the two
digests never meet. No `CACHE_VERSION` bump is needed, and that holds only while `code_version` keeps
`sys.version`.
