"""Preserve every pytest invocation, including failures, in a new evidence file."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import uuid


def main():
    destination = Path('evidence')
    destination.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    path = destination / f'tests-{stamp}-{uuid.uuid4().hex[:6]}.log'
    command = [sys.executable, '-m', 'pytest', '-vv', *sys.argv[1:]]
    with path.open('x', encoding='utf-8') as output:
        output.write(json.dumps({'command': command, 'python': sys.version}) + '\n')
        output.flush()
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding='utf-8', errors='replace')
        for line in process.stdout:
            print(line, end='', flush=True)
            output.write(line)
            output.flush()
        code = process.wait()
        output.write(f'\nEXIT_CODE={code}\n')
    print(f'Raw output: {path}', flush=True)
    raise SystemExit(code)


if __name__ == '__main__':
    main()
