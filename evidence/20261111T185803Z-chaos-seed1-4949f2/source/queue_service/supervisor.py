"""Run independent workers; replace crashed children and stop them on shutdown."""
import argparse
import os
import signal
import subprocess
import sys
import time

from queue_service.storage import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    if args.workers < 1:
        parser.error('--workers must be positive')
    Store(os.environ.get('QUEUE_DB', 'data/queue.db')).initialize()
    stopped = False

    def stop(signum, frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    command = [sys.executable, '-u', '-m', 'queue_service.worker']
    children = [subprocess.Popen(command) for _ in range(args.workers)]
    try:
        while not stopped:
            for i, process in enumerate(children):
                if process.poll() is not None:
                    print(f'Worker {process.pid} exited {process.returncode}; replacing', flush=True)
                    children[i] = subprocess.Popen(command)
            time.sleep(0.5)
    finally:
        for process in children:
            if process.poll() is None:
                process.terminate()
        for process in children:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    main()
