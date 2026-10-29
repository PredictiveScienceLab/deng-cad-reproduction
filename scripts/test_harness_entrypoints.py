"""Launcher rejection tests: synthetic paths only; no credentials or model calls."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import run_trial


class LaunchGuards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.project = self.base / 'project'
        self.runtime = self.base / 'runtime'
        self.runtime.mkdir()
        self.controller = self.project / 'sandbox/controller/v3'
        self.controller.mkdir(parents=True)
        self.auth = self.base / 'synthetic-auth.json'
        self.auth.write_text('{}')
        self.dependency = self.controller / 'dependency.txt'
        self.dependency.write_text('qualified fixture')
        (self.project / 'sandbox/reproduction-qualification.json').write_text(
            json.dumps({'passed': True, 'real_model_inference': False}))
        (self.controller / 'launch-freeze.json').write_text(json.dumps({
            'status': 'QUALIFIED_FOR_AUTHORIZED_TRIAL',
            'files': {str(self.dependency): hashlib.sha256(self.dependency.read_bytes()).hexdigest()}}))
        (self.project / 'materialization.json').write_text(json.dumps({'runtime_root': str(self.runtime)}))

    def rejected(self, arguments):
        with mock.patch.object(run_trial.subprocess, 'run') as run, \
                mock.patch.object(run_trial.subprocess, 'call') as call, \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises((SystemExit, ValueError)):
            try:
                run_trial.main(['--project-root', str(self.project), *arguments])
            finally:
                run.assert_not_called()
                call.assert_not_called()
                self.assertFalse((self.controller / 'trial-authorization.json').exists())

    def test_no_live_switch(self):
        self.rejected(['--auth-file', str(self.auth)])

    def test_no_authentication_path(self):
        self.rejected(['--live'])

    def test_authentication_inside_project(self):
        path = self.project / 'synthetic-auth.json'
        path.write_text('{}')
        self.rejected(['--live', '--auth-file', str(path)])

    def test_authentication_inside_runtime(self):
        path = self.runtime / 'synthetic-auth.json'
        path.write_text('{}')
        self.rejected(['--live', '--auth-file', str(path)])

    def test_symlink_cannot_hide_runtime_authentication(self):
        inside = self.runtime / 'synthetic-auth.json'
        inside.write_text('{}')
        outside = self.base / 'auth-link.json'
        outside.symlink_to(inside)
        self.rejected(['--live', '--auth-file', str(outside)])

    def test_dependency_tampering_precedes_transport(self):
        self.dependency.write_text('changed')
        self.rejected(['--live', '--auth-file', str(self.auth)])

    def test_failed_local_qualification(self):
        (self.project / 'sandbox/reproduction-qualification.json').write_text(
            json.dumps({'passed': False, 'real_model_inference': False}))
        self.rejected(['--live', '--auth-file', str(self.auth)])

    def test_existing_trial_is_not_overwritten(self):
        (self.project / 'trials/trial-001').mkdir(parents=True)
        self.rejected(['--live', '--auth-file', str(self.auth)])

    def test_overlapping_runtime_is_rejected(self):
        (self.project / 'materialization.json').write_text(json.dumps({'runtime_root': str(self.project)}))
        self.rejected(['--live', '--auth-file', str(self.auth)])


if __name__ == '__main__':
    unittest.main()
