"""Bound one owned VM even when its invoking runner exits unexpectedly."""
import argparse
import signal
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=1800)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not 0 < args.seconds <= 1800 or not args.command:
        raise SystemExit('Requires a command and a bounded positive timeout')
    process = None
    cancelled = False
    def interrupted(signum, frame):
        nonlocal cancelled
        cancelled = True
    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, interrupted)
    try:
        process = subprocess.Popen(args.command)
        deadline = time.monotonic() + args.seconds
        while True:
            if cancelled:
                return 143
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return 124
            try:
                return process.wait(timeout=min(.2, remaining))
            except subprocess.TimeoutExpired:
                pass
    finally:
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(signum, signal.SIG_IGN)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=5)


if __name__ == '__main__':
    raise SystemExit(main())
