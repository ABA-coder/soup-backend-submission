"""Launch a real Python PID, including inside a Windows virtual environment."""
import os
import subprocess
import sys


def python_process(arguments, *, env=None, **kwargs):
    executable = sys.executable
    environment = dict(os.environ if env is None else env)
    if os.name == 'nt' and sys.prefix != sys.base_prefix:
        # Same technique used by multiprocessing.popen_spawn_win32: avoid
        # the venv redirector, whose PID is NOT the interpreter's PID.
        executable = sys._base_executable
        environment['__PYVENV_LAUNCHER__'] = sys.executable
    return subprocess.Popen([executable, *arguments], env=environment, **kwargs)
