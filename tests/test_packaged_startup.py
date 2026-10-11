import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


class QtStartupCheckTests(unittest.TestCase):
    def test_check_runs_real_qt_without_importing_any_hardware_backend(self):
        root = Path(__file__).resolve().parents[1]
        code = """
import runpy
import sys
sys.path.insert(0, sys.argv[1])
sys.argv = [sys.argv[1] + '/stellaris15gen3.py', '--check-qt']
try:
    runpy.run_path(sys.argv[0], run_name='__main__')
except SystemExit as result:
    assert result.code == 0, result.code
else:
    raise AssertionError('Qt check did not exit')
assert not any(name == 'backend' or name.startswith('backend.') for name in sys.modules)
"""
        with TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, '-c', code, str(root)], cwd=directory,
                capture_output=True, text=True, timeout=15,
            )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
