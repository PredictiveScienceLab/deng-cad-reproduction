# Reproduction qualification

These checks validate the relocated repository package, after the original model trial. No additional model inference was performed.

| Check | Result |
| --- | --- |
| Standard-library scoring and tamper/layout tests | 15 passed |
| Relocated harness qualification | 10 batches passed |
| Harness/entrypoint unit tests within those batches | 105 passed |
| OS worker isolation probes | 61 passed |
| Actual CLI with fixed-response model fixtures | 7 passed |
| Worker deadlines, snapshot timing, simulated preflight, shutdown signals | Passed |
| Fresh restricted CAD rebuild and STEP measurements | Passed |
| Recomputed combined score | 60/61; S3D9 unresolved; all 8 gates pass |

[Harness summary](harness/summary.json) and adjacent logs describe the final qualification. [Replay qualification](replay-qualification.json) records the fresh native replay, worker reaping, unchanged rubric/snapshot bindings, and reuse of historical AI-assisted operator review. The operator review was not repeated independently.

[Tested source freeze](../../harness/tested-source-freeze.json) binds the exact relocated harness source. [Path rebase provenance](harness/path-rebase-provenance.json) records both original and packaged log hashes. Logs have temporary paths replaced for portability; numerical results are unchanged.

The copied-runtime setup path was exercised on macOS arm64. Availability of the official CLI archives was verified, but a clean download and installation from all package indexes was not tested. The reproduction does not silently substitute newer dependency versions.

Portable checks:

```sh
python3 scripts/replay.py --mode score
python3 -m unittest discover -s tests -v
python3 -m unittest discover -s scripts -p 'test_harness_entrypoints.py' -v
```
