"""Wire constants shared by operation definitions and bus handlers."""
KINDS = ['msg', 'ask', 'answer', 'ruling', 'handoff', 'fyi', 'lesson', 'debt', 'fail', 'signal', 'ack',
         'apply_request', 'hold', 'unhold', 'state', 'blocker', 'decision']
STATES = ('RUNNING', 'WAITING', 'DONE', 'FAILED', 'IDLE')
BLOCKER_CLOSERS = ('decision', 'answer')  # a later row of these kinds with --re <blocker id> closes it
# Structured signals. Waiters that trigger writes match
# the `signal` field of kind=signal posts only.
WAKE_KINDS = 'ask,handoff,ruling,fail,apply_request,blocker,decision'
TEXT_CAP = 400
