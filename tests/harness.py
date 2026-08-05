"""Shared scaffolding for running main() without touching the outside world.

Every external dependency is replaced: Google Drive authentication and uploads, both
enhancement APIs, and auto-orientation (which would otherwise load InsightFace and
add seconds per case). Scans are generated into a temp directory that is removed
afterwards, so nothing reads or writes ~/Pictures, Drive, or any real API.

What remains unstubbed is the logic under test — filename sequencing, prefix routing
and API-key validation — running inside the genuine main().
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import photo_scanner as ps  # noqa: E402

_PATCHED = ('SCANNER_OUTPUT', 'XAI_API_KEY', 'TOPAZ_API_KEY', 'authenticate_drive',
            'get_last_sequence_number', 'upload_to_drive', 'auto_orient_file',
            'load_config_file', 'enhance_with_xai', 'enhance_with_topaz',
            'enhance_photo_pipeline')


@contextlib.contextmanager
def sandbox(filenames, last_seq=0, xai_key='stub', topaz_key='stub'):
    """Run main() against generated scans in a temp dir. Yields a result dict.

    The result holds `uploaded` (names passed to Drive), `remaining` (files left on
    disk, i.e. photos that failed and were correctly kept) and `output` (stdout).
    """
    saved = {name: getattr(ps, name) for name in _PATCHED}
    tmp = tempfile.mkdtemp(prefix='photo-scanner-test-')
    result = {'uploaded': [], 'remaining': [], 'output': ''}

    try:
        for name in filenames:
            pixels = np.random.default_rng(0).integers(60, 200, (240, 320, 3)).astype(np.uint8)
            cv2.imwrite(os.path.join(tmp, name), pixels)

        def fake_enhance(src, dst, *args, **kwargs):
            shutil.copyfile(src, dst)
            return True

        ps.SCANNER_OUTPUT = tmp
        ps.XAI_API_KEY = xai_key
        ps.TOPAZ_API_KEY = topaz_key
        ps.authenticate_drive = lambda: object()
        ps.get_last_sequence_number = lambda service, date, folder: last_seq
        ps.upload_to_drive = lambda service, path, name, folder: result['uploaded'].append(name)
        ps.auto_orient_file = lambda path: 0
        ps.load_config_file = lambda: {'folder_id': 'stub'}
        ps.enhance_with_xai = fake_enhance
        ps.enhance_with_topaz = fake_enhance
        ps.enhance_photo_pipeline = lambda src, dst, colorize=False: fake_enhance(src, dst)

        sys.argv = ['photo_scanner.py', '--folder-id', 'stub']
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            ps.main()

        result['output'] = buffer.getvalue()
        result['remaining'] = sorted(f for f in os.listdir(tmp) if f.lower().endswith('.jpg'))
        yield result
    finally:
        for name, value in saved.items():
            setattr(ps, name, value)
        shutil.rmtree(tmp, ignore_errors=True)


def originals(result):
    """Uploaded filenames excluding the _ai derivatives."""
    return [name for name in result['uploaded'] if '_ai' not in name]


def first_error(result):
    return next((line.strip() for line in result['output'].splitlines() if 'ERROR' in line), None)


class Report:
    """Collects pass/fail across cases and sets the process exit code."""

    def __init__(self, title):
        self.title = title
        self.passed = 0
        self.failed = 0
        print(f"\n{title}")
        print('-' * len(title))

    def check(self, label, ok, detail=''):
        self.passed += bool(ok)
        self.failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if detail and not ok:
            print(f"        {detail}")

    def finish(self):
        total = self.passed + self.failed
        print(f"\n  {self.passed}/{total} passed")
        return 0 if self.failed == 0 else 1
