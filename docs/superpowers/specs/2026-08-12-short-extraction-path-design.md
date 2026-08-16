# Short Extraction Path Design

## Problem

The packaged application can run from a deeply nested directory. Store Insight ZIP members are currently extracted beneath the final output tree, adding `_work/all_extracted/<archive folders>/<source filename>` to an already long path. On Windows systems with long paths disabled, a projected path of 260 characters fails at `Path.open()` with a misleading `FileNotFoundError`.

## Design

`materialize()` will extract each downloaded ZIP into a `TemporaryDirectory` under the operating system temporary directory. It will classify, hash, and copy supported files into the existing final output folders while the temporary directory is alive. The context manager will remove the temporary extraction tree after copying, including when processing raises an exception.

The final output layout, ZIP traversal protection, filename sanitization, duplicate handling, manifests, and user-visible files remain unchanged. The fix does not require enabling the Windows long-path policy or moving the application.

## Verification

A regression test will observe the real extraction target, verify it is outside the deeply nested output root, verify copied output remains valid after temporary cleanup, and verify the extraction directory is removed. Existing collector tests and the full test suite will run before rebuilding the portable release.
