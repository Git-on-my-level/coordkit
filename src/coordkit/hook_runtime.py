"""Small hook deadline, available before importing delivery and manifest code."""
import contextlib
import signal
import time

HOOK_TIMEOUT = 5


@contextlib.contextmanager
def hook_deadline(timeout=None):
    """Bound nested invocations without extending an existing deadline."""
    timeout = HOOK_TIMEOUT if timeout is None else timeout
    def expired(signum, frame):
        raise TimeoutError('hook deadline')
    previous = signal.signal(signal.SIGALRM, expired)
    timer = signal.setitimer(signal.ITIMER_REAL, 0)
    signal.setitimer(signal.ITIMER_REAL, min(timeout, timer[0]) if timer[0] else timeout)
    started = time.monotonic()
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if timer[0]:
            signal.setitimer(signal.ITIMER_REAL, max(.001, timer[0] - (time.monotonic() - started)), timer[1])
