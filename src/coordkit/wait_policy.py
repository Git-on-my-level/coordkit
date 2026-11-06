"""Pure opt-in timeout recommendation and shared bus kind classification."""
import argparse
import math

DEFAULT_BUS_KINDS = 'ask,handoff,fail,ruling,apply_request,blocker,decision'


def wake_worthy(message, kinds=DEFAULT_BUS_KINDS):
    """Kind gate only. Routing, acknowledgements and deduplication remain caller duties."""
    if isinstance(kinds, str):
        kinds = None if kinds == 'all' else set(kinds.split(','))
    return kinds is None or message.get('kind', '') in kinds


def timeout_minutes(policy, requested, idle_rounds=0):
    """Grow only after consecutive empty waits; caller resets rounds on useful work."""
    if not math.isfinite(requested) or requested <= 0 or type(idle_rounds) is not int or idle_rounds < 0:
        raise ValueError('timeout must be finite and positive; idle rounds must be nonnegative')
    # Bound exponential growth so a forgotten reset cannot exceed one day.
    base = max(requested, policy.min_timeout_minutes)
    if policy.backoff == 1 or idle_rounds == 0:
        return base
    if base >= 1440 or idle_rounds * math.log(policy.backoff) >= math.log(1440) - math.log(base):
        return max(base, 1440)
    try:
        return base * policy.backoff ** idle_rounds
    except OverflowError:
        return math.exp(math.log(base) + idle_rounds * math.log(policy.backoff))


def main(argv, manifest):
    p = argparse.ArgumentParser(prog='coordkit wait ' + argv[0])
    if argv[0] == 'policy':
        p.add_argument('--timeout', type=float, default=manifest.wait.timeout_minutes)
        p.add_argument('--idle-rounds', type=int, default=0)
        o = p.parse_args(argv[1:])
        print(f'{timeout_minutes(manifest.wait, o.timeout, o.idle_rounds):g}')
        return 0
    p.add_argument('kind')
    o = p.parse_args(argv[1:])
    worthy = wake_worthy({'kind': o.kind}, manifest.wait.kinds)
    print('wake-worthy' if worthy else 'ignore')
    return 0 if worthy else 3
