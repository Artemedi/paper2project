# PDF isolation and snapshot checks: validation

Validated on 2026-11-18 against public repository base `d159c01`.
Version remains 1.0.2. No release tag or registry publication is implied.

## Changes

- PDF text extraction runs in a fresh Linux interpreter. Address space is capped
  at 512 MiB, CPU at 15 seconds and each output file at 32 MiB. The parent kills
  and reaps the process after 30 seconds or a Python-level interruption.
- Original source bytes, page markers, NUL replacement and the explicit lack of
  visual review are preserved. Missing dependencies and rejected PDFs remain visible.
- Both registered snapshots are hashed again before candidate starts. Changed or
  unreadable snapshots block candidate and retain the completed baseline result.
- Existing exact numeric comparisons, container cleanup/recovery, handoff evidence
  handling and atomic exports were preserved. These changes are a targeted port.

## Checks

- Ubuntu WSL, Python 3.14.4, pypdf 6.18.1: **38 tests passed, zero skipped**
  (15.767 seconds). This includes real PDF-byte fixtures, a compressed stream
  expanding to 600 MiB, kernel resource limits, timeout/interruption cleanup,
  changed/deleted snapshot files and an unchanged-snapshot positive control.
- Offline selftest: **PASS**, seven checks.
- Ruff 0.14.3, rules F, src/tests/tools/examples: **PASS**.
- Wheel and sdist: **PASS**, built from the public tree with `uv build --offline`.
- Installed wheel PDF import: **PASS**, isolated interpreter with no source
  PYTHONPATH, using a separate environment for this repository.
- Release ZIP: **PASS**, 23 entries, including the PDF worker. SHA-256:
  `42e8d937a4c85970ea16f3aa5d55542db2d61bc0ca3d6d8b4ca213e54b43b89c`.
- Staged diff and whitespace: **PASS**, manually inspected before commit.

## Limits

The parser process is resource-limited, not filesystem/network-sandboxed; it runs
under the user's account. Parent-enforced deadlines require the parent to remain
operational. No visual or scientific paper review is implied by parser fixtures.
Snapshot rechecking does not close the same-user check/start race. No real-Podman
qualification was run in this environment; container tests here use mocked arms.
Existing support and evidence limitations remain documented in the product guide.
