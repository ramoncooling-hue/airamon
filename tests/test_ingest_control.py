import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.ingest import control


@pytest.mark.parametrize('portable', [False, True])
def test_progress_reads_preserve_complete_child_output(portable):
    if not portable and not hasattr(os, 'pread'):
        pytest.skip('pread branch is POSIX-only; seek/read is tested on all platforms')
    progress = []
    code = (
        'import sys,time\n'
        'for page in range(1, 5):\n'
        '    sys.stderr.write("x" * 70000 + "\\n")\n'
        '    sys.stderr.write(\'{"event":"progress","page":\' + str(page))\n'
        '    sys.stderr.flush()\n'
        '    time.sleep(.03)\n'
        '    sys.stderr.write(\',"total":4}\\n\')\n'
        '    sys.stderr.flush()\n'
        'print("recognized text")\n'
    )
    expected = ''.join('x' * 70000 + '\n' +
        '{"event":"progress","page":' + str(page) + ',"total":4}\n'
        for page in range(1, 5)).replace('\n', os.linesep)
    with patch.object(control.os, 'pread', None if portable else os.pread, create=True), \
            control.controlled(lambda: False, lambda *value: progress.append(value)):
        result = control.run([sys.executable, '-c', code], timeout=10, check=True)
    assert result.stdout.strip() == 'recognized text'
    assert result.stderr == expected
    assert progress == [('ocr', page, 4) for page in range(1, 5)]


@pytest.mark.parametrize('reason', ['cancel', 'timeout'])
def test_windows_child_is_reaped_after_cancel_or_timeout(reason):
    children, progress = [], []
    popen = subprocess.Popen

    def spawn(*args, **kwargs):
        assert kwargs['start_new_session'] is False
        proc = popen(*args, **kwargs)
        children.append(proc)
        return proc

    windows_os = SimpleNamespace(name='nt', pread=None, path=os.path)
    code = 'import sys,time\nprint(\'{"event":"progress","page":1,"total":4}\', file=sys.stderr, flush=True)\ntime.sleep(60)'
    error = control.IngestCancelled if reason == 'cancel' else subprocess.TimeoutExpired
    with patch.object(control, 'os', windows_os), \
            patch.object(control.subprocess, 'Popen', side_effect=spawn), \
            control.controlled(lambda: reason == 'cancel' and bool(progress),
                               lambda *value: progress.append(value)), \
            pytest.raises(error):
        control.run([sys.executable, '-c', code], timeout=1 if reason == 'timeout' else 5)
    assert children[0].returncode is not None
    assert progress == [('ocr', 1, 4)]
