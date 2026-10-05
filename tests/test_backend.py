import builtins
import copy
import csv
import importlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


def prepared_case():
    case = {
        'id': 'synthetic',
        'geometry': {'cell': {'Lx_m': .01, 'Ly_m': .02, 'height_m': .002},
                     'regions': [{'id': 'bottom', 'material': 'a', 'kind': 'brick',
                                  'bounds_m': [[0, 0, 0], [.004, .006, .001]]},
                                 {'id': 'top', 'material': 'b', 'kind': 'brick',
                                  'bounds_m': [[0, 0, .001], [.004, .006, .002]]}]},
        'materials': {'a': {'time_convention': 'exp(+j\u03c9t)', 'frequency_unit': 'GHz',
            'epsilon_mu_table': [{'frequency': 1, 'epsilon_real': 2, 'epsilon_imag': -4, 'mu_real': 1, 'mu_imag': -.2},
                                 {'frequency': 2, 'epsilon_real': 3, 'epsilon_imag': -2, 'mu_real': 1, 'mu_imag': -.1}],
            'measured_ranges_Hz': {'epsilon': [1e9, 2e9], 'mu': [1e9, 2e9]},
            'conductivity_S_m': 2, 'epsilon_includes_conductivity': True, 'density_kg_m3': 1000},
            'b': {'time_convention': 'exp(+j\u03c9t)', 'frequency_unit': 'GHz',
            'epsilon_mu_table': [{'frequency': 1, 'epsilon_real': 4, 'epsilon_imag': -.1, 'mu_real': 1, 'mu_imag': 0},
                                 {'frequency': 2, 'epsilon_real': 4, 'epsilon_imag': -.1, 'mu_real': 1, 'mu_imag': 0}],
            'measured_ranges_Hz': {'epsilon': [1e9, 2e9], 'mu': [1e9, 2e9]}}},
        'scenario': {'profile': 'periodic-pec', 'frequencies_Hz': [1e9, 2e9],
                     'theta_deg': 0, 'azimuth_deg': 0, 'polarization': 'TE',
                     'air_height_m': .01, 'reference_plane_m': .005},
        'mesh': {'max_edge_m': .0005},
        'analysis': {'band_Hz': [1e9, 2e9], 'max_gap_Hz': 1e9},
    }
    return rebuild_fixture(case)


def rebuild_fixture(case):
    from cst_absorber.contracts import build_plan
    physical = {key: copy.deepcopy(case[key]) for key in ('id', 'geometry', 'materials', 'scenario', 'mesh', 'analysis')}
    runtime = {'max_modes': 16, 'max_cpus': 2, 'min_available_RAM_GiB': 0,
               'min_free_disk_GiB': 0, 'wall_budget_seconds': 120, 'max_attempts': 1}
    plan = build_plan({'schema_version': '1.0', 'case': physical, 'runtime': runtime}, Path('.'))
    result = plan['cases'][0]
    result['runtime'] = plan['runtime']
    return result


class BackendTests(unittest.TestCase):
    def setUp(self):
        try:
            self.backend = importlib.import_module('cst_absorber.backend')
        except ModuleNotFoundError:
            self.backend = None
        self.assertIsNotNone(self.backend, 'offline CST preparation must be implemented')

    def prepare(self, case, directory):
        original_import = builtins.__import__

        def block_native(name, *args, **kwargs):
            if name == 'cst' or name.startswith('cst.'):
                raise AssertionError('offline preparation imported the native SDK')
            return original_import(name, *args, **kwargs)

        with patch('builtins.__import__', side_effect=block_native):
            return self.backend.prepare_cst(case, Path(directory))

    def test_preparation_remains_not_run_with_hashed_review_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            case = prepared_case()
            result = self.prepare(case, directory)
            self.assertEqual(result['status'], 'prepared')
            self.assertEqual(result['validation'], 'not_run')
            self.assertFalse(result['live_supported'])
            self.assertGreater(len(result['artifacts']), 6)
            for name, digest in result['artifact_sha256'].items():
                self.assertTrue((Path(directory) / name).is_file())
                self.assertEqual(len(digest), 64)
            saved = json.loads((Path(directory) / 'preparation.json').read_text())
            self.assertEqual(saved['case_signature'], case['signature'])
            self.assertNotIn('completed', saved['status'])

    def test_sparse_geometry_preserves_explicit_cell_and_periodic_pec(self):
        with tempfile.TemporaryDirectory() as directory:
            self.prepare(prepared_case(), directory)
            setup = (Path(directory) / 'setup.vba').read_text()
            self.assertIn('.UnitCellFitToBoundingBox "False"', setup)
            self.assertIn('.UnitCellDs1 "10"', setup)
            self.assertIn('.UnitCellDs2 "20"', setup)
            self.assertIn('.Zmin "electric"', setup)
            self.assertIn('.Xmin "unit cell"', setup)
            self.assertIn('.SetPeriodicBoundaryAnglesDirection "inward"', setup)

    def test_material_fit_uses_positive_losses_without_double_sigma(self):
        with tempfile.TemporaryDirectory() as directory:
            self.prepare(prepared_case(), directory)
            text = (Path(directory) / 'materials.vba').read_text()
            self.assertIn('.AddDispersionFittingValueEps "1", "2", "4", "1"', text)
            self.assertIn('.AddDispersionFittingValueMu "1", "1", "0.2", "1"', text)
            self.assertIn('.Sigma "0"', text)
            self.assertNotIn('.Sigma "2"', text)
            self.assertNotIn('.Mue ', text)
            self.assertIn('.Rho "1000"', text)
            self.assertIn('.UseGeneralDispersionMu "True"', text)
            with (Path(directory) / 'expected_materials.csv').open(newline='') as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(float(rows[0]['epsilon_loss']), 4)
            self.assertEqual(float(rows[0]['native_sigma_S_m']), 0)
            self.assertEqual(float(rows[0]['input_sigma_S_m']), 2)

    def test_native_bricks_keep_region_material_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            self.prepare(prepared_case(), directory)
            text = (Path(directory) / 'geometry.vba').read_text()
            self.assertEqual(text.count('With Brick'), 2)
            self.assertIn('.Name "region_bottom"', text)
            self.assertIn('.Material "mat_a"', text)
            self.assertIn('.Name "region_top"', text)
            self.assertIn('.Material "mat_b"', text)
            audit = (Path(directory) / 'geometry_readback.vba').read_text()
            self.assertIn('Solid.GetMaterialNameForShape("absorber:region_top")', audit)
            self.assertIn('Solid.GetVolume("absorber:region_bottom")', audit)

    def test_frequency_list_and_mode_names_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            self.prepare(prepared_case(), directory)
            text = (Path(directory) / 'setup.vba').read_text()
            self.assertEqual(text.count('.AddSampleInterval '), 2)
            self.assertIn('.AddSampleInterval "1", "1", "1", "Single", "False"', text)
            self.assertIn('.AddMode "TE", "0", "0"', text)
            self.assertIn('.AddMode "TM", "0", "0"', text)
            self.assertIn('.AddToExcitationList "Zmax", "TE(0,0)"', text)
            self.assertIn('FloquetPort.GetModeNameByNumber',
                          (Path(directory) / 'mode_readback.vba').read_text())

    def test_global_solver_range_matches_explicit_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            self.prepare(prepared_case(), directory)
            text = (Path(directory) / 'setup.vba').read_text()
            self.assertIn('Solver.FrequencyRange "1", "2"', text)

    def test_reference_plane_air_offset_converts_to_zmax_deembedding(self):
        with tempfile.TemporaryDirectory() as directory:
            self.prepare(prepared_case(), directory)
            text = (Path(directory) / 'setup.vba').read_text()
            self.assertIn('.SetDistanceToReferencePlane "-5"', text)
            expected = json.loads((Path(directory) / 'expected_geometry.json').read_text())
            self.assertAlmostEqual(expected['reference_plane']['zref_m'], .007)
            self.assertAlmostEqual(expected['reference_plane']['zport_m'], .012)
            self.assertAlmostEqual(expected['reference_plane']['deembedding_distance_m'], -.005)
            self.assertIn('reference_plane_readback', self.backend.get_capabilities()['blocked_capabilities'])

    def test_reference_plane_outside_air_region_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for offset in [-.001, .011]:
                case = prepared_case()
                case['scenario']['reference_plane_m'] = offset
                with self.assertRaisesRegex(ValueError, 'reference'):
                    self.prepare(case, directory)

    def test_contract_identifiers_with_dots_are_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            case = prepared_case()
            case['id'] = 'case.1'
            case['geometry']['regions'][0]['id'] = 'bottom.1'
            case['geometry_audit']['volumes_m3']['bottom.1'] = case['geometry_audit']['volumes_m3'].pop('bottom')
            case['materials']['a.1'] = case['materials'].pop('a')
            case['material_samples']['a.1'] = case['material_samples'].pop('a')
            case['geometry']['regions'][0]['material'] = 'a.1'
            case = rebuild_fixture(case)
            try:
                result = self.prepare(case, directory)
            except ValueError as error:
                self.fail('contract-valid dotted identifiers must prepare: ' + str(error))
            self.assertEqual(result['case_id'], 'case.1')
            self.assertIn('.Name "region_bottom.1"', (Path(directory) / 'geometry.vba').read_text())

    def test_stl_is_staged_in_transformed_mm_with_closed_volume(self):
        import hashlib
        import trimesh
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.stl'
            trimesh.creation.box(extents=[10, 10, 1]).export(source)
            case = prepared_case()
            case['geometry']['regions'] = [{'id': 'mesh', 'material': 'a', 'kind': 'stl',
                'path': str(source), 'source_unit': 'mm',
                'transform': {'scale': 1, 'translation_m': [.005, .005, .0005]}}]
            case['geometry_audit'] = {'overlap_status': 'verified_no_overlap',
                'volumes_m3': {'mesh': 1e-7},
                'asset_hashes': {'mesh': hashlib.sha256(source.read_bytes()).hexdigest()}}
            case = rebuild_fixture(case)
            result = self.prepare(case, root / 'prepared')
            staged = trimesh.load_mesh(root / 'prepared' / 'assets' / 'region_mesh.stl')
            self.assertTrue(staged.is_watertight)
            self.assertAlmostEqual(staged.volume, 100, places=5)
            self.assertAlmostEqual(staged.bounds[0][2], 0, places=5)
            self.assertAlmostEqual(staged.bounds[1][2], 1, places=5)
            text = (root / 'prepared' / 'geometry.vba').read_text()
            self.assertIn('.ImportFileUnits "mm"', text)
            self.assertIn('.ImportToActiveCoordinateSystem "False"', text)
            self.assertIn('Solid.ChangeMaterial "absorber:region_mesh", "mat_a"', text)
            self.assertIn('assets/region_mesh.stl', result['artifact_sha256'])

    def test_mutated_stl_after_audit_is_rejected(self):
        import trimesh
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'changed.stl'
            trimesh.creation.box(extents=[10, 10, 1]).export(path)
            case = prepared_case()
            case['geometry']['regions'] = [{'id': 'mesh', 'material': 'a', 'kind': 'stl',
                'path': str(path), 'source_unit': 'mm',
                'transform': {'scale': 1, 'translation_m': [.005, .005, .0005]}}]
            case = rebuild_fixture(case)
            trimesh.creation.box(extents=[8, 10, 1]).export(path)
            with self.assertRaisesRegex(ValueError, 'changed'):
                self.prepare(case, Path(directory) / 'prepared')

    def test_unverified_overlap_and_unfolded_canonical_material_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            case = prepared_case()
            case['geometry_audit']['overlap_status'] = 'unverified'
            with self.assertRaisesRegex(ValueError, 'overlap'):
                self.prepare(case, directory)
            case = prepared_case()
            case['material_samples']['a']['conductivity_S_m'] = 2
            with self.assertRaisesRegex(ValueError, 'conductivity'):
                self.prepare(case, directory)

    def test_missing_samples_frequency_mismatch_and_vba_injection_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            for mutate in [lambda c: c.pop('material_samples'),
                           lambda c: c['material_samples']['a'].update(frequencies_Hz=[1e9]),
                           lambda c: c['geometry']['regions'][0].update(id='x"\nEnd With')]:
                case = prepared_case(); mutate(case)
                with self.assertRaises(ValueError):
                    self.prepare(case, directory)

    def test_existing_artifacts_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            self.prepare(prepared_case(), directory)
            with self.assertRaises(FileExistsError):
                self.prepare(prepared_case(), directory)

    def test_tampered_inputs_signature_and_derived_fields_are_rejected(self):
        mutators = [lambda c: c['geometry']['regions'][0]['bounds_m'][1].__setitem__(0, .003),
                    lambda c: c.update(signature='0' * 64),
                    lambda c: c['geometry_audit']['volumes_m3'].update(bottom=2.5e-8),
                    lambda c: c['material_samples']['a']['epsilon_real'].__setitem__(0, 3),
                    lambda c: c['region_masses_kg'].update(bottom=1),
                    lambda c: c['materials']['a']['epsilon_mu_table'][0].update(epsilon_real=5)]
        for mutate in mutators:
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as directory:
                case = prepared_case(); mutate(case)
                with self.assertRaisesRegex(ValueError, 'changed|mismatch'):
                    self.prepare(case, directory)

    def test_optional_required_modes_are_independently_checked(self):
        from cst_absorber.modal import required_modes
        with tempfile.TemporaryDirectory() as directory:
            case = prepared_case()
            case['required_modes'] = required_modes(case)
            try:
                result = self.prepare(case, Path(directory) / 'valid')
            except ValueError as error:
                self.fail('independently valid CLI mode metadata must prepare: ' + str(error))
            self.assertEqual(result['status'], 'prepared')
            case['required_modes'] = case['required_modes'][:1]
            with self.assertRaisesRegex(ValueError, 'required_modes.*mismatch'):
                self.prepare(case, Path(directory) / 'invalid')

    def test_real_cli_prepare_preserves_not_run(self):
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = prepared_case()
            physical = {key: case[key] for key in ('id', 'geometry', 'materials', 'scenario', 'mesh', 'analysis')}
            config = root / 'input.json'
            config.write_text(json.dumps({'schema_version': '1.0', 'case': physical, 'runtime': case['runtime']}), encoding='utf-8')
            cli = Path(__file__).resolve().parents[1] / 'scripts' / 'absorber_cli.py'
            result = subprocess.run([sys.executable, str(cli), 'prepare-cst', str(config), '--out-dir', str(root / 'out')],
                                    capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            receipt = json.loads(result.stdout)['cases'][0]['receipt']
            self.assertEqual(receipt['status'], 'prepared')
            self.assertEqual(receipt['validation'], 'not_run')
            self.assertFalse(receipt['live_supported'])

    def test_live_worker_returns_specific_blockers_without_sdk_access(self):
        with tempfile.TemporaryDirectory() as directory:
            original_import = builtins.__import__
            def forbidden(name, *args, **kwargs):
                if name == 'cst' or name.startswith('cst.'):
                    raise AssertionError('preview worker must not import CST')
                return original_import(name, *args, **kwargs)
            with patch('builtins.__import__', side_effect=forbidden):
                result = self.backend.CstBackend()(prepared_case(), Path(directory), threading.Event())
            self.assertEqual(result['status'], 'unsupported_validation')
            self.assertTrue(result['owned_closed'])
            self.assertEqual(result['validation'], 'not_run')
            self.assertFalse(result['session_created'])
            self.assertIn('gamma_units', result['blocked_capabilities'])
            self.assertIn('material_fit_readback', result['blocked_capabilities'])
            self.assertNotIn('spectra.csv', result['artifacts'])

    def test_authorized_public_backend_delegates_worker_and_ownership(self):
        from unittest.mock import Mock
        delegate = Mock()
        delegate.return_value = {'status': 'failed', 'owned_closed': True}
        delegate.last_receipt = delegate.return_value
        delegate.ownership.return_value = ({'pid': 42}, {'pid': 42})
        with patch('cst_absorber.live_backend.ExecutableCstBackend', return_value=delegate) as factory:
            worker = self.backend.CstBackend(authorized=True, exclusive_resources=True,
                                            acceptance_run=True)
            result = worker({'id': 'case'}, Path('.'), threading.Event())
            self.assertEqual(result, delegate.return_value)
            self.assertEqual(worker.last_receipt, delegate.last_receipt)
            self.assertEqual(worker.ownership(Path('.')), delegate.ownership.return_value)
            worker.cleanup_identity({'pid': 42})
            delegate.cleanup_identity.assert_called_once_with({'pid': 42})
            factory.assert_called_once_with(authorized=True, exclusive_resources=True,
                                           acceptance_run=True)

    def test_preparation_distinguishes_requested_fd_policy_and_fit_source(self):
        with tempfile.TemporaryDirectory() as directory:
            self.prepare(prepared_case(), directory)
            acceptance = json.loads((Path(directory) / 'acceptance_requirements.json').read_text())
            self.assertIn('material_solver_policy', acceptance,
                          'requested FD interpolation policy needs its own provenance')
            policy = acceptance['material_solver_policy']
            self.assertFalse(policy['requested_TDCompatibleMaterials'])
            self.assertEqual(policy['documented_table_treatment'], 'volumetric_response_undocumented')
            self.assertEqual(policy['native_setting_readback'], 'unavailable_public_getter')
            self.assertEqual(policy['solver_response_linkage'], 'unverified')
            self.assertFalse(acceptance['material_response_source_roles']['roles_interchangeable'])
            self.assertEqual(acceptance['material_response_source_roles']['FD - Interpolated'],
                             'fd_interpolated_response')
            self.assertEqual(acceptance['material_response_source_roles']['Fit'], 'nth_order_fit_response')

    def test_prepared_units_use_documented_setunit_interface(self):
        with tempfile.TemporaryDirectory() as directory:
            self.prepare(prepared_case(), directory)
            setup = (Path(directory) / 'setup.vba').read_text()
            self.assertIn('.SetUnit "Length", "mm"', setup)
            self.assertIn('.SetUnit "Frequency", "GHz"', setup)
            self.assertNotIn('.Geometry "mm"', setup)


if __name__ == '__main__':
    unittest.main()
