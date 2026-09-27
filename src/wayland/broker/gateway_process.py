"""Keep the vendor process tied to its owning Wayland service, including crashes."""

import select
import subprocess
import sys


def main():
    child = subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL)
    try:
        while child.poll() is None:
            readable, _, _ = select.select([sys.stdin.buffer], [], [], 0.5)
            if readable and not sys.stdin.buffer.read(1):
                break  # Owner died or explicitly closed the lifetime pipe.
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)


if __name__ == "__main__":
    main()
