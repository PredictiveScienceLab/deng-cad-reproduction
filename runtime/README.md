# Runtime setup

The qualified environment is macOS arm64, standalone CPython3.12.14,
Codex CLI0.164.0 with its matching `codex-code-mode-host`, and the47 pinned
distributions in `requirements-lock.txt`. Runtime binaries are not bundled.

The simplest local verification path copies an existing complete runtime:

```sh
python3 scripts/setup_runtime.py --destination /absolute/new-runtime --runtime-source /absolute/existing-runtime
```

The source layout must contain `python/`, `venv/`, and `cli/`. Only the Python
and scientific-environment trees plus the two named CLI binaries are copied.
The script rebases venv metadata/internal symlinks, rejects external symlink
targets, validates all pinned versions, and records binary hashes. It performs
no network downloads and never reads login files. APFS copy-on-write is used
where available. The destination must be new and needs roughly2.1GiB logically.

For a standalone installation, obtain a self-contained, relocatable CPython
3.12.14 distribution for aarch64-apple-darwin and the two matching CLI binaries.
[uv's managed Python documentation](https://docs.astral.sh/uv/concepts/python-versions/)
describes acquiring a specific standalone Python version. Use a directory
containing `bin/python3.12`, not a Homebrew symlink to an interpreter elsewhere.
The official [Codex 0.164.0 release](https://github.com/openai/codex/releases/tag/rust-v0.164.0)
provides separate macOS arm64 archives for
[Codex](https://github.com/openai/codex/releases/download/rust-v0.164.0/codex-aarch64-apple-darwin.tar.gz)
and the [Code Mode host](https://github.com/openai/codex/releases/download/rust-v0.164.0/codex-code-mode-host-aarch64-apple-darwin.tar.gz).
Their availability and archive SHA256 digests were verified against the official
GitHub release API on 2026-10-29; see `official-assets.json`. Download and unpack
both, verify those archive digests, then place the executables together under
the names `codex` and `codex-code-mode-host`. The archive contents were not
downloaded during repository preparation; the tested binaries came from the
existing Homebrew Cask installation of this same version.

```sh
python3 scripts/setup_runtime.py --destination /absolute/new-runtime --install --python-source /absolute/standalone-python-root --codex-source /absolute/codex-binaries
```

`--install` explicitly permits pip to fetch the pinned scientific distributions.
This route needs package-index access and matching wheels; unavailable historical
versions are an installation failure, not permission to silently upgrade. The
repository's validation used the copied-runtime route; a fresh download/install
has not been independently tested. The standalone installer uses the supplied
Python's venv/pip tooling and verifies the resulting package versions.

Both paths produce `runtime-manifest.json` and the same runtime layout:

```
runtime/
  python/bin/python3.12
  venv/bin/python
  cli/codex
  cli/codex-code-mode-host
  runtime-manifest.json
```

Version pins reproduce the dependency selection, not an archival wheel mirror.
Setup records executable hashes; local sandbox qualification remains mandatory
after installation or relocation. Never add auth files, HOME directories,
session caches, or model-service keys to the runtime tree.
