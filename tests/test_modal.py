import math
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

try:
    from cst_absorber.modal import required_modes, mode_state
except ImportError:
    required_modes = mode_state = None

C = 299792458.0

class ModalTests(unittest.TestCase):
    def case(self, lx=.01, ly=.01, frequencies=None, angle=0):
        return {'geometry': {'cell': {'Lx_m': lx, 'Ly_m': ly}},
                'scenario': {'frequencies_Hz': frequencies or [1e9, 2e9], 'theta_deg': angle, 'azimuth_deg': 0, 'polarization': 'TE'}}

    def test_rectangular_lattice_enumerates_outside_configured_set(self):
        self.assertIsNotNone(required_modes, 'independent modal enumerator missing')
        case = self.case(1, .1, [1e9])
        case['scenario']['configured_modes'] = [{'m': 0, 'n': 0, 'polarization': 'TE'}]
        modes = required_modes(case)
        keys = {(x['m'], x['n'], x['polarization']) for x in modes}
        self.assertIn((3, 0, 'TM'), keys)
        self.assertNotIn((0, 1, 'TE'), keys)

    def test_oblique_and_large_period_enumeration(self):
        self.assertIsNotNone(required_modes)
        keys = {(x['m'], x['n']) for x in required_modes(self.case(2, 1, [1e9], 45))}
        self.assertIn((-10, 0), keys)
        self.assertNotIn((10, 0), keys)

    def test_cutoff_is_required_but_not_propagating(self):
        self.assertIsNotNone(required_modes)
        case = self.case(1, .1, [C])
        self.assertIn({'m': 1, 'n': 0, 'polarization': 'TE'}, required_modes(case))
        self.assertEqual(mode_state(case, C, 1, 0)['state'], 'cutoff')

    def test_runtime_limit_does_not_silently_truncate(self):
        self.assertIsNotNone(required_modes)
        case = self.case(1, 1, [1e9]); case['runtime'] = {'max_modes': 2}
        with self.assertRaisesRegex(ValueError, 'max_modes'):
            required_modes(case)
