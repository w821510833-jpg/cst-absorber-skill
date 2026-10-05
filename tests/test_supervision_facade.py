"""Facade protocol regressions; no vendor SDK or process operations."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from cst_absorber.backend import CstBackend


class SupervisionFacadeTests(unittest.TestCase):
    def test_supervision_status_preserves_unknown_owned_state(self):
        class Delegate:
            def supervision_status(self):
                return {'session_created': 'unknown', 'owned_closed': False,
                        'request_pending': True}
        facade = object.__new__(CstBackend)
        facade._delegate = Delegate()
        self.assertTrue(callable(getattr(facade, 'supervision_status', None)),
                        'facade must expose the durable supervision status protocol')
        self.assertEqual(facade.supervision_status(),
                         {'session_created': 'unknown', 'owned_closed': False,
                          'request_pending': True})

    def test_cleanup_receipt_and_seconds_are_not_reinterpreted(self):
        class Delegate:
            def supervise_cleanup(self, *, timeout_seconds):
                if timeout_seconds != 2.5:
                    raise ValueError('seconds budget was not forwarded')
                return {'owned_closed': False, 'reason': 'pending SDK request'}
        facade = object.__new__(CstBackend)
        facade._delegate = Delegate()
        self.assertTrue(callable(getattr(facade, 'supervise_cleanup', None)),
                        'facade must expose seconds-bounded owned cleanup')
        self.assertEqual(facade.supervise_cleanup(timeout_seconds=2.5),
                         {'owned_closed': False, 'reason': 'pending SDK request'})


if __name__ == '__main__':
    unittest.main()
