import copy
import importlib
import math
import unittest
from test_contracts import case_fixture


class MaterialTests(unittest.TestCase):
    def setUp(self):
        try:
            self.module = importlib.import_module('cst_absorber.materials')
        except ModuleNotFoundError:
            self.module = None
        self.assertIsNotNone(self.module, 'material sampling must be implemented')
        self.material = case_fixture()['materials']['a']

    def test_piecewise_linear_units_and_loss_convention(self):
        sampled = self.module.sample_material(self.material, [1.5e9])
        self.assertAlmostEqual(sampled['epsilon_real'][0], 2.5)
        self.assertAlmostEqual(sampled['epsilon_imag'][0], -.15)
        self.material['time_convention'] = 'exp(-jωt)'
        for row in self.material['epsilon_mu_table']:
            row['epsilon_imag'] *= -1
        other = self.module.sample_material(self.material, [1.5e9])
        self.assertEqual(sampled['epsilon_imag'], other['epsilon_imag'])

    def test_conductivity_is_folded_once(self):
        self.material['conductivity_S_m'] = 1
        sampled = self.module.sample_material(self.material, [1e9])
        expected = -.1 - 1 / (2 * math.pi * 1e9 * 8.8541878128e-12)
        self.assertAlmostEqual(sampled['epsilon_imag'][0], expected)
        self.material['epsilon_includes_conductivity'] = True
        sampled = self.module.sample_material(self.material, [1e9])
        self.assertEqual(sampled['epsilon_imag'], [-.1])
        self.assertEqual(sampled['conductivity_S_m'], 0)

    def test_passive_sign_density_and_invalid_numbers(self):
        for density in [0, -1, math.inf, 10**1000, True, '1000']:
            material = copy.deepcopy(self.material)
            material['density_kg_m3'] = density
            with self.assertRaises(ValueError):
                self.module.sample_material(material, [1e9])
        self.material['epsilon_mu_table'][0]['epsilon_imag'] = .1
        with self.assertRaises(ValueError):
            self.module.sample_material(self.material, [1e9])

    def test_coverage_and_authorized_linear_extrapolation(self):
        with self.assertRaises(ValueError):
            self.module.sample_material(self.material, [2.5e9])
        self.material['extrapolation'] = {'allowed': True, 'range_Hz': [1e9, 3e9],
                                          'method': 'linear', 'authorization': 'synthetic test'}
        self.assertAlmostEqual(self.module.sample_material(self.material, [2.5e9])['epsilon_real'][0], 3.5)
        with self.assertRaises(ValueError):
            self.module.sample_material(self.material, [3.1e9])
        self.material['extrapolation']['authorization'] = ''
        with self.assertRaises(ValueError):
            self.module.sample_material(self.material, [2.5e9])

    def test_measured_mu_coverage_is_independent_and_missing_density_explicit(self):
        self.material['measured_ranges_Hz']['mu'] = [1e9, 1.5e9]
        with self.assertRaises(ValueError):
            self.module.sample_material(self.material, [2e9])
        self.material.pop('density_kg_m3')
        result = self.module.sample_material(self.material, [1e9])
        self.assertFalse(result['mass_available'])
        self.assertIsNone(result['density_kg_m3'])


if __name__ == '__main__':
    unittest.main()
