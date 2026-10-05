import copy
import importlib
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


def case_fixture():
    return {
        'id': 'synthetic',
        'geometry': {'cell': {'Lx_m': .01, 'Ly_m': .01, 'height_m': .002},
                     'regions': [{'id': 'layer', 'material': 'a', 'kind': 'brick',
                                  'bounds_m': [[0, 0, 0], [.01, .01, .001]]}]},
        'materials': {'a': {'time_convention': 'exp(+jωt)', 'frequency_unit': 'GHz',
                           'epsilon_mu_table': [
                               {'frequency': 1, 'epsilon_real': 2, 'epsilon_imag': -.1,
                                'mu_real': 1, 'mu_imag': 0},
                               {'frequency': 2, 'epsilon_real': 3, 'epsilon_imag': -.2,
                                'mu_real': 1, 'mu_imag': 0}],
                           'measured_ranges_Hz': {'epsilon': [1e9, 2e9], 'mu': [1e9, 2e9]},
                           'density_kg_m3': 1000}},
        'scenario': {'profile': 'periodic-pec', 'frequencies_Hz': [1e9, 1.5e9, 2e9],
                     'theta_deg': 0, 'azimuth_deg': 0, 'polarization': 'TE',
                     'air_height_m': .01, 'reference_plane_m': .005},
        'mesh': {'max_edge_m': .0005},
        'analysis': {'band_Hz': [1e9, 2e9], 'max_gap_Hz': 5e8}}


def config_fixture():
    return {'schema_version': '1.0', 'case': case_fixture(), 'runtime': {
        'max_cpus': 2, 'min_available_RAM_GiB': 1, 'min_free_disk_GiB': 1,
        'max_modes': 16, 'wall_budget_seconds': 120}}


class ContractTests(unittest.TestCase):
    def setUp(self):
        try:
            self.module = importlib.import_module('cst_absorber.contracts')
        except ModuleNotFoundError:
            self.module = None
        self.assertIsNotNone(self.module, 'configuration contracts must be implemented')

    def test_single_defaults_and_prepared_density_mass(self):
        plan = self.module.build_plan(config_fixture(), Path('.'))
        self.assertEqual(plan['scope_mode'], 'single')
        self.assertEqual(len(plan['cases']), 1)
        self.assertEqual(plan['runtime']['max_attempts'], 1)
        self.assertAlmostEqual(plan['cases'][0]['region_masses_kg']['layer'], .0001)

    def test_batch_requires_confirmation_and_finite_explicit_cases(self):
        raw = config_fixture()
        raw['cases'] = [raw.pop('case'), dict(case_fixture(), id='second')]
        for execution in [{}, {'mode': 'batch'}, {'mode': 'batch', 'confirmed': False}]:
            raw['execution'] = execution
            with self.assertRaises(ValueError):
                self.module.normalize_config(raw, Path('.'))
        raw['execution'] = {'mode': 'batch', 'confirmed': True}
        self.assertEqual(self.module.build_plan(raw, Path('.'))['scope_mode'], 'batch')
        raw['cases'] = raw['cases'][:1]
        self.assertEqual(self.module.build_plan(raw, Path('.'))['scope_mode'], 'single')

    def test_plural_parameters_and_unknown_physical_fields_fail(self):
        for section, key, value in [('scenario', 'theta_deg', [0, 15]),
                                    ('scenario', 'polarization', ['TE', 'TM']),
                                    ('mesh', 'max_edges_m', [.001, .002]),
                                    ('scenario', 'azimuth_deg', 15)]:
            raw = config_fixture()
            raw['case'][section][key] = value
            with self.assertRaises(ValueError):
                self.module.normalize_config(raw, Path('.'))

    def test_malformed_numbers_frequencies_and_ids_fail(self):
        for value in [True, '2', math.nan, math.inf, 10**1000, 0, -1]:
            raw = config_fixture()
            raw['case']['mesh']['max_edge_m'] = value
            with self.assertRaises(ValueError):
                self.module.normalize_config(raw, Path('.'))
        for frequencies in [[2e9, 1e9], [1e9, 1e9], [], [True], '1 GHz']:
            raw = config_fixture()
            raw['case']['scenario']['frequencies_Hz'] = frequencies
            with self.assertRaises(ValueError):
                self.module.normalize_config(raw, Path('.'))
        raw = config_fixture()
        raw['execution'] = {'mode': 'batch', 'confirmed': True}
        raw['cases'] = [raw.pop('case'), case_fixture()]
        with self.assertRaises(ValueError):
            self.module.normalize_config(raw, Path('.'))

    def test_hash_is_key_order_stable_and_content_sensitive(self):
        self.assertEqual(self.module.canonical_hash({'a': 1, 'b': 2}),
                         self.module.canonical_hash({'b': 2, 'a': 1}))
        first = self.module.build_plan(config_fixture(), Path('.'))
        raw = config_fixture()
        raw['runtime']['wall_budget_seconds'] = 999
        second = self.module.build_plan(raw, Path('.'))
        self.assertEqual(first['cases'][0]['signature'], second['cases'][0]['signature'])
        raw['case']['materials']['a']['epsilon_mu_table'][0]['epsilon_real'] = 4
        third = self.module.build_plan(raw, Path('.'))
        self.assertNotEqual(first['cases'][0]['signature'], third['cases'][0]['signature'])
        self.assertNotEqual(first['content_hash'], third['content_hash'])
        with self.assertRaises(ValueError):
            self.module.canonical_hash({'a': math.nan})

    def test_hash_normalizes_equivalent_json_numbers(self):
        self.assertEqual(self.module.canonical_hash({'value': [1, 0]}),
                         self.module.canonical_hash({'value': [1.0, -0.0]}))

    def test_two_material_densities_and_unavailable_mass(self):
        raw = config_fixture()
        case = raw['case']
        case['geometry']['regions'].append({'id': 'upper', 'material': 'b', 'kind': 'brick',
                                            'bounds_m': [[0, 0, .001], [.01, .01, .002]]})
        case['materials']['b'] = copy.deepcopy(case['materials']['a'])
        case['materials']['b']['density_kg_m3'] = 2000
        result = self.module.build_plan(raw, Path('.'))['cases'][0]
        self.assertAlmostEqual(result['region_masses_kg']['layer'], .0001)
        self.assertAlmostEqual(result['region_masses_kg']['upper'], .0002)
        case['materials']['b'].pop('density_kg_m3')
        result = self.module.build_plan(raw, Path('.'))['cases'][0]
        self.assertIsNone(result['region_masses_kg']['upper'])

    def test_normalization_rejects_unsupported_geometry_fields(self):
        raw = config_fixture()
        raw['case']['geometry']['regions'][0]['rotation'] = [0, 0, 90]
        with self.assertRaises(ValueError):
            self.module.normalize_config(raw, Path('.'))

    def test_case_region_and_material_ids_reject_path_escape_and_newlines(self):
        for invalid in ['../escape', 'unsafe\nname', ' leading', '.hidden', 'x' * 101]:
            for target in ['case', 'region', 'material']:
                raw = config_fixture()
                if target == 'case':
                    raw['case']['id'] = invalid
                elif target == 'region':
                    raw['case']['geometry']['regions'][0]['id'] = invalid
                else:
                    raw['case']['materials'][invalid] = raw['case']['materials'].pop('a')
                    raw['case']['geometry']['regions'][0]['material'] = invalid
                with self.assertRaises(ValueError):
                    self.module.normalize_config(raw, Path('.'))

    def test_stl_content_identity_excludes_asset_location(self):
        import tempfile
        import trimesh
        with tempfile.TemporaryDirectory() as directory:
            box = trimesh.creation.box(extents=[.002, .002, .0005])
            first_path = Path(directory) / 'first.stl'
            second_path = Path(directory) / 'second.stl'
            box.export(first_path)
            second_path.write_bytes(first_path.read_bytes())
            raw = config_fixture()
            raw['case']['geometry']['regions'] = [{'id': 'layer', 'material': 'a', 'kind': 'stl',
                'path': 'first.stl', 'source_unit': 'm',
                'transform': {'scale': 1, 'translation_m': [.005, .005, .0005]}}]
            first = self.module.build_plan(raw, Path(directory))
            raw['case']['geometry']['regions'][0]['path'] = 'second.stl'
            second = self.module.build_plan(raw, Path(directory))
            self.assertEqual(first['content_hash'], second['content_hash'])
            raw['case']['geometry']['regions'][0]['transform']['scale'] = 1.1
            self.assertNotEqual(first['content_hash'], self.module.build_plan(raw, Path(directory))['content_hash'])

    def test_relative_stl_and_omitted_transform_prepare_absolute_defaults(self):
        import tempfile
        import trimesh
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            box = trimesh.creation.box(extents=[.002, .002, .0005])
            box.apply_translation([.001, .001, .00025])
            box.export(base_dir / 'synthetic.stl')
            raw = config_fixture()
            raw['case']['geometry']['regions'] = [{'id': 'layer', 'material': 'a',
                'kind': 'stl', 'path': 'synthetic.stl', 'source_unit': 'm'}]
            implicit = self.module.build_plan(raw, base_dir)
            region = implicit['cases'][0]['geometry']['regions'][0]
            self.assertEqual(region['path'], str((base_dir / 'synthetic.stl').resolve()))
            self.assertEqual(region['transform'], {'scale': 1.0, 'translation_m': [0.0, 0.0, 0.0]})
            raw['case']['geometry']['regions'][0]['transform'] = {'scale': 1, 'translation_m': [0, 0, 0]}
            explicit = self.module.build_plan(raw, base_dir)
            self.assertEqual(implicit['cases'][0]['signature'], explicit['cases'][0]['signature'])
            self.assertEqual(implicit['content_hash'], explicit['content_hash'])
            backend = importlib.import_module('cst_absorber.backend')
            preparation = backend.prepare_cst(implicit['cases'][0], base_dir / 'offline-preparation')
            self.assertEqual(preparation['status'], 'prepared')
            self.assertEqual(preparation['validation'], 'not_run')


if __name__ == '__main__':
    unittest.main()
