"""Run every test module. Exits non-zero if any case fails.

    python tests/run_all.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MODULES = ['test_naming.py', 'test_keys.py']

failed = 0
for module in MODULES:
    result = subprocess.run([sys.executable, os.path.join(HERE, module)], cwd=HERE)
    failed += result.returncode != 0

print()
print('ALL TESTS PASSED' if failed == 0 else f'{failed} test module(s) FAILED')
sys.exit(1 if failed else 0)
