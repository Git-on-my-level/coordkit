"""Read the shared machine-line contract; producers own these files and their freshness."""
from collections import deque
import re

PREFIX = re.compile(r'^(FLAG_DUE_SOON|FLAG_OVERDUE_COUNT|FLAG_DARK_OVER_14D|QUIET_PRS_OVER_14D|'
                    r'APPROVED_NOT_OWNED|OPEN_COUNTS)\b')


def lines(manifest):
    directories = [manifest.paths.root/'state'/'machine']
    paths = set()
    external = manifest.paths.machine_state
    if external is not None:
        directories.extend([external/'machine', external/'lane-watch'/'machine'])
        paths.add(external/'quiet-prs.lines')
    for directory in directories:
        try:
            paths.update(directory.glob('*.lines'))
        except OSError:  # optional producer directories must never block the lead digest
            continue
    result = deque(maxlen=20)
    for path in sorted(paths):
        try:
            with path.open(encoding='utf-8', errors='replace') as stream:
                for raw in stream:
                    line = raw.rstrip('\r\n')
                    if PREFIX.match(line):
                        result.append(line)
        except OSError:  # missing, unreadable or replaced producer files do not block delivery
            continue
    return list(result)
