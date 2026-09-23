"""Offline checks for environment selection and truthful setup readiness."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('swan_setup', Path(__file__).resolve().parents[1] / 'sidm/tools/swan.py')
swan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(swan)


class SwanSetupTests(unittest.TestCase):
    def test_casa_does_not_modify_authentication(self):
        with patch.dict(os.environ, {'XrdSecPROTOCOL': 'existing'}, clear=True), patch.object(swan, 'is_swan', return_value=False):
            before = dict(os.environ)
            self.assertFalse(swan.configure_notebook())
            self.assertEqual(dict(os.environ), before)

    def test_swan_with_wrong_kernel_has_actionable_error(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(swan, 'is_swan', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'Python \\(SIDM SWAN\\)'):
                swan.configure_notebook()

    def test_expired_proxy_clears_ready_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path/'ready.json').write_text(json.dumps({'ready': True, 'proxy': '/tmp/test-proxy'}))
            with patch.dict(os.environ, {'RUNNING_ON_SWAN': 'true', 'SIDM_SWAN_READY': 'true', 'X509_USER_PROXY': '/tmp/test-proxy', 'XrdSecPROTOCOL': 'gsi,unix'}, clear=True), patch.object(swan, 'is_swan', return_value=True), patch.object(swan, 'runtime_directory', return_value=path), patch.object(swan, 'proxy_lifetimes', side_effect=RuntimeError('expired')):
                with self.assertRaisesRegex(RuntimeError, 'expired'):
                    swan.configure_notebook()
                self.assertEqual(os.environ['SIDM_SWAN_READY'], 'false')

    def test_failed_read_never_registers_ready_kernel(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with patch.object(swan, 'is_swan', return_value=True), patch.object(swan, 'runtime_directory', return_value=path), patch.object(swan, 'voms_environment', return_value={'X509_USER_PROXY': str(path/'proxy')}), patch.object(swan, 'proxy_lifetimes', return_value=[3600, 1800]), patch.object(swan.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'probe')):
                with self.assertRaises(subprocess.CalledProcessError):
                    swan.setup(use_existing=True)
                self.assertFalse(json.loads((path/'ready.json').read_text())['ready'])
                self.assertFalse((path/'environment.sh').exists())

    def test_192_hour_request_and_kerberos_free_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            cert, key, proxy = [path/name for name in ('cert', 'key', 'proxy')]
            cert.touch(); key.touch(mode=0o600)
            calls = []
            def run(command, **kwargs):
                calls.append((command, kwargs))
                return subprocess.CompletedProcess(command, 0)
            installed = {}
            def install(**kwargs):
                installed.update(kwargs)
                return str(path)
            fake = types.ModuleType('ipykernel.kernelspec')
            fake.install = install
            env = {'X509_USER_CERT': str(cert), 'X509_USER_KEY': str(key), 'X509_USER_PROXY': str(proxy)}
            with patch.object(swan, 'is_swan', return_value=True), patch.object(swan, 'runtime_directory', return_value=path), patch.object(swan, 'voms_environment', return_value=env), patch.object(swan, 'proxy_lifetimes', return_value=[691200, 86400]), patch.object(swan.subprocess, 'run', side_effect=run), patch.dict(sys.modules, {'ipykernel.kernelspec': fake}):
                swan.setup()
                self.assertIn('--valid', calls[0][0])
                self.assertEqual(calls[0][0].count('192:00'), 2)
                probe_env = calls[1][1]['env']
                self.assertEqual(probe_env['XrdSecPROTOCOL'], 'gsi,unix')
                self.assertFalse(Path(probe_env['KRB5CCNAME'].removeprefix('FILE:')).exists())
                self.assertEqual(installed['env']['RUNNING_ON_SWAN'], 'true')
                self.assertTrue(json.loads((path/'ready.json').read_text())['ready'])
                with patch.dict(os.environ, installed['env'], clear=True):
                    self.assertTrue(swan.configure_notebook())
                    self.assertEqual(os.environ['SIDM_SWAN_READY'], 'true')


if __name__ == '__main__':
    unittest.main()
