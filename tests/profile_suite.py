"""Time the offline suite on Python 3.11: PYTHONPATH=src python3.11 -m tests.profile_suite."""
import argparse
import sys
import time
import unittest


class TimedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timings = []

    def startTest(self, test):
        self.started = time.perf_counter()
        super().startTest(test)

    def stopTest(self, test):
        self.timings.append((time.perf_counter()-self.started,test.id()))
        super().stopTest(test)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-seconds',type=float,default=120)
    args = parser.parse_args()
    started = time.perf_counter()
    suite = unittest.defaultTestLoader.discover('tests',top_level_dir='.')
    result = unittest.TextTestRunner(verbosity=1,resultclass=TimedResult).run(suite)
    elapsed = time.perf_counter()-started
    print(f'SUITE_SECONDS {elapsed:.3f} budget={args.max_seconds:g}')
    for seconds,name in sorted(result.timings,reverse=True)[:10]:
        print(f'TEST_SECONDS {seconds:.3f} {name}')
    return 0 if result.wasSuccessful() and elapsed <= args.max_seconds else 1


if __name__ == '__main__':
    sys.exit(main())
