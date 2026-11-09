"""Receipt-producing readiness check. A slow verifier is never signalled by its caller."""
import os
from pathlib import Path
import subprocess
import sys

from .state import write_json


def main(argv):
    receipt, output, *command = argv
    path = Path(output)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT)
            code = result.returncode
        except OSError:
            code = 127
    write_json(Path(receipt), dict(pid=os.getpid(), returncode=code))


if __name__ == '__main__':
    main(sys.argv[1:])
