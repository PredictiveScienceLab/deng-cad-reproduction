# Restricted live harness

This is a relocatable copy of the experiment's macOS arm64 harness. The file
layout and execution chain remain:

```
Codex0.164.0 -> local model/tool gate -> MCP relay -> trusted broker
            -> Seatbelt Python/CadQuery worker
```

The candidate sees `run_python`, `view_image`, and `check_geometry`. Arbitrary
JavaScript dispatch, built-in shell/delegation tools, and other MCP tools are
rejected by the model response gate. Workers deny network access and child
creation, permit read-only runtime/input access, and permit writes only in
candidate work/scratch. The trusted provider gateway owns credentials. Worker
processes receive an explicit clean environment with no credential variables.

The original GPT6.1 Sol/xhigh settings, four diagnostic-check limit,35-minute
checkpoint,60-minute absolute deadline, and conservative snapshot admission are
preserved. A crossing operation contributes its last quiescent baseline; late
termination-grace writes cannot enter the earlier snapshot. No time is added
back for checks, copying, or model thinking. Early completion seals the result.

## Prepare locally

1. Set up a runtime with `scripts/setup_runtime.py` (see `runtime/README.md`).
2. Materialize into a NEW private local directory:

```sh
python3 scripts/materialize.py --project-root /absolute/new-run --runtime-root /absolute/runtime
python3 scripts/qualify_harness.py --project-root /absolute/new-run
```

Qualification runs unit tests, filesystem/network/process canaries, real
restricted synthetic CadQuery workers, fixed loopback Responses fixtures,
deadline/timing probes, and signal cleanup. It does not invoke a model or
attempt the Deng task. The host DNS positive control resolves `example.com`;
candidate network probes must be denied. Run qualification from a normal local
terminal because an outer application sandbox may prohibit nested Seatbelt or
loopback server setup. An infrastructure denial is a failed qualification.

`--units-only` is useful for development but never qualifies a live launch.
Qualification writes a fresh local evaluator freeze and, only after the full
suite passes, a local launch freeze. Historical qualification is not inherited
across a relocation. All code/runtime dependencies are checked again before a
live preflight. Do not edit policies or templates after qualification without
materializing and qualifying again.

## Optional authenticated live experiment

Live access is optional. Replay and local qualification need no account or
credentials. The historical live path used an existing ChatGPT-authenticated
Codex account; its private gateway is version-specific and requires access to
the requested model. It does not substitute an API-key provider or silently
choose another model.

After local qualification, explicitly opt in:

```sh
python3 scripts/run_trial.py --project-root /absolute/new-run --live --auth-file /private/owner/auth.json
```

This authorizes a separate live synthetic transport preflight followed by the
CAD experiment. Both consume provider requests. The auth file must remain
outside both the materialized experiment and runtime. It is read only by the trusted gateway,
never copied into configuration, runtime, trial outputs, or candidate mounts.
The copied CLI gets a fresh empty HOME/CODEX_HOME with no auth. The cached login
must remain valid through the remaining fixed budget; the harness does not
refresh or export credentials. Consult the official
[Codex authentication documentation](https://learn.chatgpt.com/docs/auth) for
account setup; no credentials are included in this repository.

Calling the wrapper without `--live` fails before any provider request. Direct
provider transport also requires `DENG_ENABLE_LIVE=1`; the wrapper sets it only
after explicit opt-in and successful local qualification/freeze verification.
Each materialized project admits one `trial-001`; use a new directory for a
new experiment. Trial transcripts and broker capabilities remain private local
run data and must not be committed. Do not publish an entire live run directory.

## Scope and provenance

`provenance.json` records the original file hashes, template hashes, and
relocation changes. `@PROJECT@` and `@RUNTIME@` are rendered only by the
materializer. The runtime manifest records exact scientific package versions,
Python version, and CLI executable hashes. Current candidate inputs and the
rubric stay byte-identical to the packaged experiment artifacts.

This qualification applies to macOS arm64 and Codex0.164.0. Linux/Windows and
other CLI versions need separate isolation implementations/qualification. OS
scheduling is not a hard-real-time guarantee; confirmed reaping and baseline
snapshots enforce admission. The original supervisor does not claim protection
against abrupt trusted-controller death or OS failure. A new live model run is
not expected to reproduce the historical model output bit for bit.
