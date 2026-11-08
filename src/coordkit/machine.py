"""Read the shared machine-line contract; producers own these files and their freshness."""
from collections import deque
import re

PREFIX = re.compile(r'^(FLAG_DUE_SOON|FLAG_OVERDUE_COUNT|FLAG_DARK_OVER_14D|QUIET_PRS_OVER_14D|'
                    r'APPROVED_NOT_OWNED|OPEN_COUNTS)\b')


def lines(manifest):
    paths = set((manifest.paths.root/'state'/'machine').glob('*.lines'))
    external = manifest.paths.machine_state
    if external is not None:
        paths.update((external/'machine').glob('*.lines'))
        paths.update((external/'lane-watch'/'machine').glob('*.lines'))
        paths.add(external/'quiet-prs.lines')
    result = deque(maxlen=20)
    for path in sorted(paths):
        try:
            with path.open() as stream:
                for raw in stream:
                    line = raw.rstrip('\r\n')
                    if PREFIX.match(line):
                        result.append(line)
        except FileNotFoundError:  # producer has not published yet (or removed its snapshot)
            continue
    return list(result)
