#!/usr/bin/env python3
"""Render relocatable harness templates and create a distinct local evaluator freeze.

This does not install software, run candidate source, contact a model, or mark a
live harness qualified. The unchanged historical rubric retains inert source-path
metadata; no runtime code uses that historical path.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil

REPOSITORY = Path(__file__).resolve().parents[1]
TOKENS = ('@PROJECT@', '@RUNTIME@')

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def ordinary_files(root):
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('An ordinary input directory is required: ' + str(root))
    files = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError('Symlinks and special files are not package inputs: ' + str(path))
        if path.is_file():
            files.append(path)
    return files

def canonical(path):
    result = Path(path).expanduser().resolve()
    # Templates contain quoted Python, JSON, TOML and Seatbelt literals. Spaces
    # are supported; reject characters requiring different per-language escaping.
    if any(c in str(result) for c in ('\n', '\r', '\x00', "'", '"', '\\')):
        raise ValueError('Materialization paths cannot contain quotes, backslashes or control characters')
    return result

def validate_layout(project_root, runtime_root, *, repository=REPOSITORY):
    project, runtime = canonical(project_root), canonical(runtime_root)
    repository = Path(repository).resolve()
    if project == runtime or project in runtime.parents or runtime in project.parents:
        raise ValueError('Project and runtime roots must be disjoint; runtime read access must not expose project grading or candidate state')
    if runtime == repository or runtime in repository.parents:
        raise ValueError('Runtime root must not encompass the reproduction repository')
    for name in ('harness', 'results', 'agent-input', 'scripts', 'tests', 'article'):
        protected = repository / name
        if runtime == protected or runtime in protected.parents or protected in runtime.parents:
            raise ValueError('Runtime root overlaps packaged source/evidence: ' + name)
    return project, runtime

def freeze_quality(project_root, runtime_root):
    """Bind the relocated evaluator; call again after changing runtime metadata.

    This is deliberately a NEW local freeze, not the historical qualification.
    Callers must rerun affected qualification after any new freeze.
    """
    project, runtime = validate_layout(project_root, runtime_root)
    quality = project / 'quality'
    provenance_path = REPOSITORY / 'harness/quality-provenance.json'
    provenance = json.loads(provenance_path.read_text())
    rubric = quality / 'frozen/rubric.json'
    if sha(rubric) != provenance['rubric_sha256']:
        raise ValueError('Frozen rubric bytes changed during relocation')
    files = {}
    for folder in (quality / 'evaluator', quality / 'frozen'):
        for path in ordinary_files(folder):
            if '__pycache__' in path.parts or path.suffix == '.pyc':
                continue
            files[str(path.relative_to(quality))] = sha(path)
    readme = quality / 'README.txt'
    if readme.is_file():
        files['README.txt'] = sha(readme)
    required = [project / name for name in (
        'sandbox/controller/controller.py',
        'sandbox/controller/v2/worker_supervisor.py',
        'sandbox/controller/v2/worker.sb',
        'sandbox/control/candidate-environment.json')]
    missing = [str(path.relative_to(project)) for path in required if not path.is_file()]
    if missing:
        raise ValueError('Missing sandbox dependencies: ' + ', '.join(missing))
    dependencies = {str(path): sha(path) for path in required}
    for path in (project / 'sandbox/runtime-manifest.json', runtime / 'runtime-manifest.json'):
        if path.is_file():
            dependencies[str(path)] = sha(path)
    manifest = {
        'schema_version': 1,
        'release': 'deng-quality-v1-relocated-local',
        'status': 'FROZEN',
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'scope': 'Local path relocation; original criterion/scoring/geometry logic and exact rubric bytes preserved.',
        'files': files,
        'external_dependencies': dependencies,
        'historical_freeze_sha256': provenance['historical_freeze_sha256'],
        'historical_rubric_sha256': provenance['rubric_sha256'],
        'template_provenance_sha256': sha(provenance_path),
        'runtime_root': str(runtime),
        'runtime_manifest_present': (runtime / 'runtime-manifest.json').is_file(),
        'portability_changes': [
            'Project/runtime path literals rendered at the selected local paths.',
            'Historical report-generation and campaign-qualification drivers omitted; runtime modules, fixtures and relevant tests retained.',
            'Full source-paper PDF dependency omitted: it is provenance, not read by evaluation. Its recorded hash remains in the exact original rubric.',
            'Historical evaluation freeze identity is retained as provenance only; local qualification must be rerun.'
        ],
        'change_policy': 'Any local code/policy/runtime/rubric change invalidates this freeze and requires affected qualification. Never change criteria or tolerances in response to candidate results.'
    }
    (quality / 'freeze.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest

def materialize(project_root, runtime_root, *, repository=REPOSITORY):
    project, runtime = validate_layout(project_root, runtime_root, repository=repository)
    repository = Path(repository).resolve()
    if project.exists():
        raise FileExistsError('Choose a new project root; existing directories are never overwritten')
    for protected in (repository, *(repository / name for name in
                      ('harness', 'results', 'agent-input', 'scripts', 'tests'))):
        if project == protected or project in protected.parents or (protected != repository and protected in project.parents):
            raise ValueError('Project root must not overlap packaged source/evidence directories')
    if repository != REPOSITORY:
        raise ValueError('Alternate repository materialization requires a copied script location')
    templates = repository / 'harness/templates'
    packet = repository / 'agent-input'
    template_files, packet_files = ordinary_files(templates), ordinary_files(packet)
    project.mkdir(parents=True, mode=0o700)
    rendered = {}
    replacements = {'@PROJECT@': str(project), '@RUNTIME@': str(runtime)}
    for source in template_files:
        relative = source.relative_to(templates)
        if '__pycache__' in relative.parts or source.suffix == '.pyc':
            continue
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = source.read_bytes()
        if any(token.encode() in raw for token in TOKENS):
            content = raw.decode('utf-8')
            for token, value in replacements.items():
                content = content.replace(token, value)
            raw = content.encode('utf-8')
        target.write_bytes(raw)
        rendered[str(relative)] = sha(target)
    for relative in ('sandbox/candidate/input', 'sandbox/candidate/work', 'sandbox/candidate/tmp',
                     'sandbox/qualification', 'quality/reviews', 'trials', 'review', 'agent-input'):
        (project / relative).mkdir(parents=True, exist_ok=True)
    input_hashes = {}
    for source in packet_files:
        relative = source.relative_to(packet)
        input_hashes[str(relative)] = sha(source)
        for base in (project / 'agent-input', project / 'sandbox/candidate/input'):
            target = base / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    packet_manifest = {'schema_version': 1, 'status': 'relocated_packet_no_trial_started',
                       'candidate_input_files': input_hashes,
                       'note': 'Exact packaged participant input bytes; operator rubric/reviews are not candidate inputs.'}
    (project / 'review/packet-manifest.json').write_text(json.dumps(packet_manifest, indent=2) + '\n')
    freeze = freeze_quality(project, runtime)
    record = {'schema_version': 1, 'project_root': str(project), 'runtime_root': str(runtime),
              'rendered_files': rendered, 'input_packet_sha256': sha(project / 'review/packet-manifest.json'),
              'local_evaluator_freeze_sha256': sha(project / 'quality/freeze.json'),
              'historical_evaluator_freeze_sha256': freeze['historical_freeze_sha256'],
              'live_harness_qualified': False, 'model_inference_performed': False}
    (project / 'materialization.json').write_text(json.dumps(record, indent=2) + '\n')
    return record

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, required=True)
    parser.add_argument('--runtime-root', type=Path, required=True)
    parser.add_argument('--refresh-quality-freeze', action='store_true',
                        help='Regenerate only the local freeze after runtime metadata changes; does not qualify it')
    args = parser.parse_args()
    result = freeze_quality(args.project_root, args.runtime_root) if args.refresh_quality_freeze else materialize(args.project_root, args.runtime_root)
    print(json.dumps(result, indent=2))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
