#!/usr/bin/env python3
"""Replay the packaged Deng result without inference.

score: standard library only; verify artifact bindings and recompute the original
       score using saved independent measurements and protected operator review.
full:  macOS only; fresh isolated source rebuild and automatic STEP measurements,
       then reuse the same snapshot-bound protected operator review.
Neither mode performs or claims a fresh independent human review.
"""
from __future__ import annotations
import argparse
import ast
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys

REPOSITORY = Path(__file__).resolve().parents[1]
MAX_JSON = 32 * 1024 * 1024

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def read_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JSON:
        raise ValueError('Unsafe or oversized JSON: ' + str(path))
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key: ' + key)
            result[key] = value
        return result
    return json.loads(path.read_text(), object_pairs_hook=pairs,
                      parse_constant=lambda v: (_ for _ in ()).throw(ValueError('Nonfinite JSON: ' + v)))

def load_scoring(repository=REPOSITORY):
    path = Path(repository) / 'harness/templates/quality/evaluator/scoring.py'
    provenance = read_json(Path(repository) / 'harness/quality-provenance.json')
    if sha(path) != provenance['template_module_sha256']['scoring.py']:
        raise ValueError('Packaged scoring module changed')
    spec = importlib.util.spec_from_file_location('deng_replay_scoring', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def historical_functions(scoring, repository=REPOSITORY):
    """Reuse the exact original pure scoring adapters, without importing CAD/OS code.

    evaluate.py normally imports the macOS controller. Extracting these two
    unchanged function definitions permits Linux/Windows score replay with only
    the standard library. No submitted or model-generated source is compiled.
    """
    path = Path(repository) / 'harness/templates/quality/evaluator/evaluate.py'
    provenance = read_json(Path(repository) / 'harness/quality-provenance.json')
    if sha(path) != provenance['template_module_sha256']['evaluate.py']:
        raise ValueError('Packaged observation/gate adapter changed')
    tree = ast.parse(path.read_text(), filename=str(path))
    names = {'_observations', '_supplemental'}
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in functions} != names:
        raise ValueError('Original scoring adapters unavailable')
    namespace = {'json': json, 'evaluate_measurement': scoring.evaluate_measurement}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace['_observations'], namespace['_supplemental']

def verify_snapshot(result_root, evaluation, rubric_hash, scoring):
    root = Path(result_root).resolve(strict=True)
    output = root / 'output'
    if output.is_symlink() or not output.is_dir():
        raise ValueError('Ordinary sealed-output directory required')
    manifest = read_json(root / 'snapshot-manifest.json')
    if not isinstance(manifest.get('files'), dict):
        raise ValueError('Snapshot manifest requires file entries')
    snapshot_hash = scoring.digest_json(manifest['files'])
    if snapshot_hash != evaluation.get('snapshot_sha256'):
        raise ValueError('Historical evaluation is bound to a different snapshot')
    if evaluation.get('rubric_sha256') != rubric_hash:
        raise ValueError('Historical evaluation is bound to a different rubric')
    expected = set(manifest['files'])
    actual = set()
    for path in output.rglob('*'):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError('Output contains symlink or special file: ' + str(path))
        if stat.S_ISREG(mode):
            actual.add(path.relative_to(output).as_posix())
    if expected != actual:
        raise ValueError('Snapshot file inventory changed')
    for relative, metadata in manifest['files'].items():
        key = PurePosixPath(relative)
        if key.is_absolute() or '..' in key.parts or '\\' in relative or str(key) != relative:
            raise ValueError('Unsafe snapshot member path')
        path = output.joinpath(*key.parts)
        if path.stat().st_size != metadata['bytes'] or sha(path) != metadata['sha256']:
            raise ValueError('Snapshot file bytes changed: ' + relative)
    return snapshot_hash, manifest

def validate_saved_review(review, snapshot_hash, rubric_hash):
    """Validate trusted packaged review data; never ingest candidate-side grades."""
    if review.get('snapshot_sha256') != snapshot_hash or review.get('rubric_sha256') != rubric_hash:
        raise ValueError('Protected review is bound to a different snapshot/rubric')
    if not isinstance(review.get('reviewer'), str) or not review['reviewer'].strip():
        raise ValueError('Named reviewer required')
    if not isinstance(review.get('observations'), dict):
        raise ValueError('Observation map required')
    for entry in review['observations'].values():
        if not isinstance(entry, dict) or not isinstance(entry.get('evidence'), list) or not entry['evidence'] or not all(isinstance(e, str) and e.strip() for e in entry['evidence']):
            raise ValueError('Review measurements require textual evidence')
        if 'override_reason' in entry and (not isinstance(entry['override_reason'], str) or not entry['override_reason'].strip()):
            raise ValueError('Automatic override requires a reason')
        entry['origin'] = 'operator:' + review['reviewer']
    permitted = {'SUP-SEPARATE-STAND', 'SUP-SUPPORT-DEPENDENCIES', 'SUP-CUMULATIVE-GEOMETRY', 'SUP-STAND-DIMENSIONS', 'GATE-EDITABLE-SOURCE'}
    supplemental = review.get('supplemental', {})
    if not isinstance(supplemental, dict) or set(supplemental) - permitted:
        raise ValueError('Unknown supplemental review gate')
    for cid, entry in supplemental.items():
        if not isinstance(entry, dict) or not isinstance(entry.get('evidence'), list) or not entry['evidence'] or not all(isinstance(e, str) and e.strip() for e in entry['evidence']):
            raise ValueError('Supplemental review requires textual evidence')
        if cid == 'SUP-STAND-DIMENSIONS':
            if not isinstance(entry.get('components'), dict):
                raise ValueError('Stand dimensions require measured components')
        elif type(entry.get('satisfied')) is not bool:
            raise ValueError('Supplemental relation requires exact Boolean')
    return review

def compare_recorded(computed, recorded):
    """Compare outcomes rather than copied paths/provenance presentation strings."""
    issues = []
    for field in ('confirmed_correct', 'denominator', 'status_counts', 'confirmed_attempted', 'all_criteria_correct'):
        if computed['score'].get(field) != recorded['score'].get(field):
            issues.append('score.' + field)
    if computed['status'] != recorded.get('status'):
        issues.append('status')
    current = {row['id']: row['status'] for row in computed['score']['criteria']}
    historical = {row['id']: row['status'] for row in recorded['score']['criteria']}
    if current != historical:
        issues.append('criterion statuses')
    if {g['id']: g['status'] for g in computed['gates']} != {g['id']: g['status'] for g in recorded['gates']}:
        issues.append('gate statuses')
    return issues

def score_replay(result_root, *, repository=REPOSITORY):
    result_root, repository = Path(result_root).resolve(strict=True), Path(repository).resolve(strict=True)
    scoring = load_scoring(repository)
    extract, supplemental = historical_functions(scoring, repository)
    rubric_path = repository / 'harness/templates/quality/frozen/rubric.json'
    rubric_hash = sha(rubric_path)
    provenance = read_json(repository / 'harness/quality-provenance.json')
    if rubric_hash != provenance['rubric_sha256']:
        raise ValueError('Exact historical rubric bytes changed')
    rubric = read_json(rubric_path)
    scoring.validate_rubric(rubric)
    result_provenance = read_json(result_root / 'provenance.json')
    bound_files = result_provenance.get('package_json_sha256', {})
    for required in ('evaluation.json', 'review.json', 'reviewed-evaluation.json'):
        if bound_files.get(required) != sha(result_root / required):
            raise ValueError('Packaged result metadata changed: ' + required)
    evaluation = read_json(result_root / 'evaluation.json')
    snapshot_hash, _ = verify_snapshot(result_root, evaluation, rubric_hash, scoring)
    if result_provenance.get('snapshot_sha256') != snapshot_hash:
        raise ValueError('Packaged provenance is bound to a different snapshot')
    review_path = result_root / 'review.json'
    if review_path.resolve().is_relative_to((result_root / 'output').resolve()):
        raise ValueError('Review cannot be candidate output')
    review = validate_saved_review(read_json(review_path), snapshot_hash, rubric_hash)
    if 'component_pose_review' in review and review['component_pose_review']['poses'] != evaluation['analysis'].get('integrity', {}).get('component_poses', {}):
        raise ValueError('Changed component poses require native remeasurement')
    observations = extract(evaluation['analysis'], rubric)
    overrides = []
    for cid, entry in review['observations'].items():
        if cid in observations:
            if not entry.get('override_reason'):
                raise ValueError('Automatic override requires a reason: ' + cid)
            overrides.append(cid)
        observations[cid] = entry
    score = scoring.score(rubric, observations)
    gates = supplemental(rubric, evaluation['analysis'], evaluation['rebuild'], review)
    gates.extend(g for g in evaluation['gates'] if g['id'] == 'GATE-DOCUMENTATION')
    accepted = score['all_criteria_correct'] and all(g['status'] == 'pass' for g in gates)
    status = 'pass' if accepted else 'fail' if any(g['status'] == 'fail' for g in gates) or score['status_counts']['incorrect'] or score['status_counts']['absent'] else 'requires_review'
    result = {
        'schema_version': 1, 'mode': 'score', 'status': status, 'score': score, 'gates': gates,
        'snapshot_sha256': snapshot_hash, 'rubric_sha256': rubric_hash,
        'historical_evaluator_freeze_sha256': provenance['historical_freeze_sha256'],
        'recorded_evaluation_sha256': sha(result_root / 'evaluation.json'),
        'packaged_result_provenance_sha256': sha(result_root / 'provenance.json'),
        'operator_review_sha256': sha(review_path), 'automatic_overrides': overrides,
        'fresh_geometry_measured': False, 'candidate_source_executed': False,
        'model_inference_performed': False,
        'scope': 'Recomputed scoring from packaged historical independent measurements and protected review. This verifies arithmetic/bindings, not new CAD measurements or independent validation of reviewer judgments.'}
    expected = result_root / 'reviewed-evaluation.json'
    result['recorded_outcome_differences'] = compare_recorded(result, read_json(expected))
    return result

def full_replay(result_root, project_root, runtime_root, *, seconds=180, repository=REPOSITORY):
    if sys.platform != 'darwin':
        raise RuntimeError('Full replay requires macOS Seatbelt; use --mode score on this platform')
    baseline = score_replay(result_root, repository=repository)
    if baseline['recorded_outcome_differences']:
        raise ValueError('Recorded result does not match score replay')
    from materialize import materialize, validate_layout
    project, runtime = validate_layout(project_root, runtime_root)
    python = runtime / 'venv/bin/python'
    if not python.is_file():
        raise ValueError('Installed runtime/venv/bin/python is required')
    if not project.exists():
        materialize(project, runtime)
    manifest = read_json(project / 'materialization.json')
    if manifest['runtime_root'] != str(runtime):
        raise ValueError('Materialized runtime root differs')
    root = Path(result_root).resolve(strict=True)
    workspace = project / 'replay-workspace'
    destination = project / 'replay-evaluation'
    if workspace.exists() or destination.exists():
        raise FileExistsError('Replay work/report already exists; choose a fresh project root')
    shutil.copytree(root / 'output', workspace / 'output')
    review_path = project / 'quality/reviews/replay-review.json'
    if review_path.exists():
        raise FileExistsError('Protected replay review already exists')
    shutil.copyfile(root / 'review.json', review_path)
    # Trusted orchestration only. evaluate() executes submitted source solely
    # via its no-fork Seatbelt worker and parses STEP only in a second worker.
    driver = "import json,sys\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\nfrom evaluate import evaluate\nr=evaluate(Path(sys.argv[2]),Path(sys.argv[3]),review_path=Path(sys.argv[4]),seconds=float(sys.argv[5]),writers_stopped=True)\nprint(json.dumps({'status':r['status'],'confirmed_correct':r['score']['confirmed_correct']}))\n"
    completed = subprocess.run([str(python), '-I', '-c', driver, str(project / 'quality/evaluator'),
                                str(workspace), str(destination), str(review_path), str(seconds)],
                               capture_output=True, text=True,
                               env={'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'PYTHONDONTWRITEBYTECODE': '1',
                                    'LANG': 'en_US.UTF-8', 'LC_ALL': 'en_US.UTF-8'})
    if completed.returncode:
        raise RuntimeError('Isolated evaluator failed: ' + completed.stderr[-3000:])
    evaluated = read_json(destination / 'evaluation.json')
    differences = compare_recorded(evaluated, read_json(root / 'reviewed-evaluation.json'))
    record = {
        'schema_version': 1, 'mode': 'full', 'status': evaluated['status'], 'score': evaluated['score'],
        'gates': evaluated['gates'], 'snapshot_sha256': evaluated['snapshot_sha256'],
        'rubric_sha256': evaluated['rubric_sha256'],
        'local_evaluator_freeze_sha256': evaluated['evaluator_freeze_sha256'],
        'historical_evaluator_freeze_sha256': baseline['historical_evaluator_freeze_sha256'],
        'historical_operator_review_reused': True,
        'fresh_geometry_measured': evaluated.get('analysis', {}).get('worker', {}).get('success') is True,
        'candidate_source_execution': 'Restricted no-fork worker only; source process reaped before independent STEP analysis.',
        'model_inference_performed': False, 'recorded_outcome_differences': differences,
        'evaluation_directory': str(destination),
        'scope': 'Fresh isolated rebuild/integrity and19 automatic criteria, combined with historical protected42-item review. Operator measurements and design-intent judgments are reused, not independently repeated.'}
    if record['snapshot_sha256'] != baseline['snapshot_sha256'] or record['rubric_sha256'] != baseline['rubric_sha256']:
        raise ValueError('Native replay changed snapshot/rubric binding')
    (project / 'replay-result.json').write_text(json.dumps(record, indent=2) + '\n')
    return record

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('score', 'full'), default='score')
    parser.add_argument('--result-root', type=Path, default=REPOSITORY / 'results/trial-001')
    parser.add_argument('--project-root', type=Path)
    parser.add_argument('--runtime-root', type=Path)
    parser.add_argument('--seconds', type=float, default=180)
    parser.add_argument('--output', type=Path, help='New JSON report file; never overwrites input/evidence')
    args = parser.parse_args()
    if args.seconds <= 0 or args.seconds > 600:
        parser.error('--seconds must be positive and at most600')
    if args.mode == 'full' and (args.project_root is None or args.runtime_root is None):
        parser.error('--mode full requires --project-root and --runtime-root')
    if args.output and args.output.exists():
        parser.error('--output must name a new file')
    result = score_replay(args.result_root) if args.mode == 'score' else full_replay(args.result_root, args.project_root, args.runtime_root, seconds=args.seconds)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({key: result.get(key) for key in ('mode', 'status', 'snapshot_sha256', 'rubric_sha256', 'recorded_outcome_differences')}
                     | {'confirmed_correct': result['score']['confirmed_correct'],
                        'denominator': result['score']['denominator'], 'status_counts': result['score']['status_counts'],
                        'fresh_geometry_measured': result['fresh_geometry_measured']}, indent=2))
    return 1 if result['recorded_outcome_differences'] else 0

if __name__ == '__main__':
    raise SystemExit(main())
