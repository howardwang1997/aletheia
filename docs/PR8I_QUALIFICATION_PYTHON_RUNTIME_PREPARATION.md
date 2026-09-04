# PR-8i qualification Python runtime preparation

- Status: source complete; the prepared tree was bound into qualified generation h, but preparation
  alone is not deployment qualification
- Date: 2026-09-05

## Why this stage exists

The first real Linux target probe exposed two properties that synthetic deployment fixtures did
not exercise. Python started with `-S` cannot import runtime dependencies unless one reviewed
package directory is appended after standard-library initialization, and an ordinary Conda
environment contains symlink aliases while its Python executable maps the host's ambient ELF
loader and glibc. PR-8h deliberately rejects both symlinks/hardlinks in reviewed trees and every
native object mapped outside the reviewed code/Python roots.

PR-8i adds a separate, qualification-only preparation command. It takes a canonical,
out-of-band-SHA-pinned request and:

1. hashes the complete disposable Conda source environment, including every regular file,
   directory and in-tree symlink;
2. copies it to an absent staging target while materializing aliases as independent entries;
3. copies an explicitly pinned root-owned ELF loader/glibc closure into the runtime;
4. uses a byte-pinned `patchelf` to bind Python to the in-tree loader and `$ORIGIN/../lib`;
5. runs the real interpreter with `-S -s -P`, `LC_ALL=C`, and a canonical dependency import set,
   rejecting any `/proc/self/maps` path outside the staged tree;
6. removes all write bits, freezes root ownership, proves every regular file has link count one,
   reruns the probe at the final path, and emits an exhaustive `QualificationReviewedCodeTree`.

The systemd renderer now sets `LANG=C` and `LC_ALL=C`, preventing locale/gconv mappings from
silently reintroducing ambient native dependencies. It still unsets `LD_LIBRARY_PATH` and
`LD_PRELOAD`; the closure works through the reviewed ELF interpreter/RPATH, not environment
injection.

## Operator flow

The source environment and native sources are staging inputs, not qualified artifacts. Build them
with Conda, freeze every source path/hash/mode in the request, then keep the request root-owned mode
`0400` for apply:

```console
/opt/aletheia-build/qualification-env/bin/python \
  /opt/aletheia/source/scripts/prepare-qualification-python-runtime.py \
  --request /root/qualification-python-runtime-preparation.json \
  --request-sha256 '<sha256>'

sudo /opt/aletheia-build/qualification-env/bin/python \
  /opt/aletheia/source/scripts/prepare-qualification-python-runtime.py \
  --request /root/qualification-python-runtime-preparation.json \
  --request-sha256 '<sha256>' \
  --apply \
  --acknowledge PREPARE_QUALIFICATION_PYTHON_RUNTIME
```

Use the disposable Conda interpreter to invoke both plan and apply. Do not add `-S` to this outer
preparation command: the preparation tool itself imports reviewed build-environment dependencies.
The prepared target interpreter's internal probe still always runs with `-S -s -P` and appends only
the request-bound site-packages path. The final runtime path must be absent and its parent must be
root-owned, root-group-owned and not group/world writable.

## Evidence boundary

The receipt is preparation evidence only: `deployment_qualified=false` and
`scientific_admission_allowed=false`. It does not install systemd units, create Linux principals,
commission PostgreSQL, run the destructive PR-8h campaign, or qualify ARL-1. Its exhaustive tree
must be embedded in the PR-8f seed spec and then independently rehashed by PR-8h on the exact host.

## Target-A preparation checkpoint

The 2026-08-29 target-A run used request
`71b155c27f1f418bba79e9a2001136cc0a66a8a2eeda0af4164fa54e247134f4` and emitted one canonical
JSON line. The retained stdout artifact, including its single trailing LF, has SHA-256
`a7bd36c671ea1a83ad069a9bd98ec6a67577aa7f5f963449634199312a77bfa8`; the canonical receipt
model bytes without that transport delimiter have SHA-256
`cbaff6a658d0afc0ece6081f1f20e6d46ffed84bc1484f0fd1c63a70778deb9f`. The receipt binds
manifest `9904a0cfa1cf7d49c0201f5a614ded78efadc0458157f38c6601600303ab1836`, 1,826 nested
directories, 23,810 regular files, and 79 loaded native paths. An independent post-run traversal
found zero symlinks, hardlinks, special files, writable entries, ownership mismatches, or external
native mappings; all eight requested modules imported under the final `-S -s -P` interpreter.

The preparation CLI now constructs stdout from `canonical_json_bytes(receipt) + b"\n"` directly,
and a byte-level regression fixes that framing. This preserves the historical artifact bytes while
preventing an operator from confusing a canonical JSON model digest with the digest of its
newline-delimited stdout transport.

The first attempt correctly failed while encoding legitimate inert Conda filenames containing
spaces and parentheses. The reviewed-tree grammar now admits those two printable characters while
continuing to reject control characters, quotes, variable expansion, and command separators. That
failure emitted no receipt and its output was isolated before the same request was rerun. This
checkpoint proves the preparation mechanism on the target class only; it does not change either
negative authority flag above.
