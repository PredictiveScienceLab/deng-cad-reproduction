"""Portable score replay and corruption controls; no CAD, subprocess or inference."""
from pathlib import Path
import builtins
import copy
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from replay import score_replay, full_replay
from materialize import materialize, validate_layout

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)
        self.result = self.directory / 'trial'
        shutil.copytree(ROOT / 'results/trial-001', self.result)

    def change_review(self, action):
        path = self.result / 'review.json'
        review = json.loads(path.read_text())
        action(review)
        path.write_text(json.dumps(review))
        # Deliberately changed test fixture. Rebind its metadata provenance so
        # tests below exercise scoring logic rather than only hash rejection.
        provenance = self.result / 'provenance.json'
        data = json.loads(provenance.read_text())
        data['package_json_sha256']['review.json'] = sha(path)
        provenance.write_text(json.dumps(data))

    def test_recorded_replay_recomputes_60_of_61_and_preserves_ambiguity(self):
        result = score_replay(self.result)
        self.assertEqual(result['score']['confirmed_correct'], 60)
        self.assertEqual(result['score']['denominator'], 61)
        self.assertEqual(result['status'], 'requires_review')
        self.assertEqual(result['recorded_outcome_differences'], [])
        unresolved = [x['id'] for x in result['score']['criteria'] if x['status'] != 'correct']
        self.assertEqual(unresolved, ['S3D9'])
        self.assertFalse(result['candidate_source_executed'])
        self.assertFalse(result['fresh_geometry_measured'])

    def test_score_only_requires_no_cad_or_platform_controller(self):
        original = builtins.__import__
        def restricted(name, *args, **kwargs):
            if name.split('.')[0] in {'cadquery', 'OCP', 'numpy', 'fcntl', 'controller', 'execution'}:
                self.fail('Score replay tried to import native/platform module: ' + name)
            return original(name, *args, **kwargs)
        with patch('builtins.__import__', restricted), patch('subprocess.run', side_effect=AssertionError('Subprocess forbidden')):
            self.assertEqual(score_replay(self.result)['score']['confirmed_correct'], 60)

    def test_changed_measurement_changes_score_not_just_recorded_total(self):
        self.change_review(lambda r: r['observations']['S2D1'].update(values=[9, 8, 8, 8]))
        result = score_replay(self.result)
        self.assertEqual(result['score']['confirmed_correct'], 59)
        self.assertEqual(result['score']['status_counts']['incorrect'], 1)
        self.assertIn('criterion statuses', result['recorded_outcome_differences'])

    def test_through_failure_is_not_discarded_when_diameter_matches(self):
        self.change_review(lambda r: r['observations']['S3D7'].update(conditions={'through': False}))
        result = score_replay(self.result)
        row = next(x for x in result['score']['criteria'] if x['id'] == 'S3D7')
        self.assertEqual(row['status'], 'incorrect')
        self.assertEqual(result['score']['confirmed_correct'], 59)

    def test_unknown_review_id_rejected(self):
        self.change_review(lambda r: r['observations'].update(S9D1=copy.deepcopy(r['observations']['S2D1'])))
        with self.assertRaisesRegex(ValueError, 'Unknown criterion'):
            score_replay(self.result)

    def test_unjustified_automatic_override_rejected(self):
        self.change_review(lambda r: r['observations'].update(S1D7={'values': [70], 'evidence': ['test fixture']}))
        with self.assertRaisesRegex(ValueError, 'override requires'):
            score_replay(self.result)

    def test_review_snapshot_binding_rejected(self):
        self.change_review(lambda r: r.update(snapshot_sha256='wrong'))
        with self.assertRaisesRegex(ValueError, 'different snapshot'):
            score_replay(self.result)

    def test_changed_output_bytes_rejected(self):
        with (self.result / 'output/build.py').open('a') as f:
            f.write('\nraise RuntimeError("must never execute")\n')
        with self.assertRaisesRegex(ValueError, 'bytes changed'):
            score_replay(self.result)

    def test_extra_output_file_rejected(self):
        (self.result / 'output/unrecorded.txt').write_text('extra')
        with self.assertRaisesRegex(ValueError, 'inventory changed'):
            score_replay(self.result)

    def test_changed_recorded_analysis_rejected(self):
        p = self.result / 'evaluation.json'
        p.write_text(p.read_text() + ' ')
        with self.assertRaisesRegex(ValueError, 'metadata changed'):
            score_replay(self.result)

    def test_symlink_output_rejected(self):
        if not hasattr(Path, 'symlink_to'):
            self.skipTest('Platform has no symlinks')
        target = self.result / 'output/README.txt'
        replacement = self.directory / 'readme.txt'
        target.rename(replacement)
        try:
            target.symlink_to(replacement)
        except OSError:
            self.skipTest('Symlink creation unavailable')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            score_replay(self.result)

    def test_full_replay_fails_closed_without_macos(self):
        with patch('sys.platform', 'linux'):
            with self.assertRaisesRegex(RuntimeError, 'macOS Seatbelt'):
                full_replay(self.result, self.directory / 'project', self.directory / 'runtime')

class LayoutTests(unittest.TestCase):
    def test_project_runtime_overlap_is_forbidden_both_directions(self):
        base = Path(tempfile.gettempdir()).resolve() / 'deng-layout-test'
        for project, runtime in [(base, base), (base / 'project', base), (base, base / 'runtime')]:
            with self.subTest(project=project, runtime=runtime):
                with self.assertRaisesRegex(ValueError, 'disjoint'):
                    validate_layout(project, runtime)

    def test_runtime_cannot_expose_repository_or_grading(self):
        project = Path(tempfile.gettempdir()).resolve() / 'deng-layout-test-project'
        for runtime in (ROOT, ROOT.parent, ROOT / 'results', ROOT / 'harness'):
            with self.subTest(runtime=runtime):
                with self.assertRaises(ValueError):
                    validate_layout(project, runtime)

    def test_new_materialization_keeps_rubric_and_uses_distinct_freeze(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            project, runtime = base / 'project with spaces', base / 'runtime with spaces'
            result = materialize(project, runtime)
            provenance = json.loads((ROOT / 'harness/quality-provenance.json').read_text())
            self.assertEqual(sha(project / 'quality/frozen/rubric.json'), provenance['rubric_sha256'])
            self.assertNotEqual(result['local_evaluator_freeze_sha256'], result['historical_evaluator_freeze_sha256'])
            self.assertFalse(result['live_harness_qualified'])
            self.assertNotIn('@PROJECT@', (project / 'quality/evaluator/evaluate.py').read_text())
            self.assertNotIn('@RUNTIME@', (project / 'quality/evaluator/execution.py').read_text())
            self.assertFalse((project / 'quality/reviews/replay-review.json').exists())
            with self.assertRaises(FileExistsError):
                materialize(project, runtime)

if __name__ == '__main__':
    unittest.main(verbosity=2)
