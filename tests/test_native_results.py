import builtins
import copy
import csv
import importlib
import json
import math
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


class FakeItem:
    def __init__(self, path, run, x, y, *, ref=None, parameters=None):
        self.treepath, self.run_id = path, run
        self.title, self.xlabel, self.ylabel = 'original title', 'Frequency / GHz', 'S / 1'
        self.x, self.y, self.ref = x, y, ref
        self.parameters = parameters or {'theta': 0.0}
        self.length = len(y) if isinstance(y, list) else 1

    def get_data(self):
        if not isinstance(self.y, list):
            return self.y
        if self.ref is None:
            return list(zip(self.x, self.y))
        return list(zip(self.x, self.y, self.ref))

    def get_xdata(self):
        return self.x

    def get_ydata(self):
        return self.y

    def get_ref_imp_data(self):
        return self.ref

    def get_parameter_combination(self):
        return copy.deepcopy(self.parameters)


class FakeResults:
    def __init__(self, items):
        self.items, self.calls = items, []
        self.module_parameters = {'theta': 0.0}

    def ProjectFile(self, path, *, allow_interactive):
        self.calls.append(('ProjectFile', path, allow_interactive))
        if allow_interactive is not False:
            raise AssertionError('interactive access was requested')
        return self

    def get_3d(self):
        self.calls.append(('get_3d',))
        return self

    def get_tree_items(self):
        return list(self.items)

    def get_run_ids(self, path):
        self.calls.append(('get_run_ids', path))
        return list(self.items[path])

    def get_parameter_combination(self, run_id):
        return copy.deepcopy(self.module_parameters)

    def get_result_item(self, path, *, run_id, load_impedances):
        self.calls.append(('get_result_item', path, run_id, load_impedances))
        item = self.items[path][run_id]
        if isinstance(item, Exception):
            raise item
        return item


class ResultsTestBase(unittest.TestCase):
    def setUp(self):
        try:
            self.reader = importlib.import_module('cst_absorber.native_results')
        except ModuleNotFoundError:
            self.reader = None
        self.assertIsNotNone(self.reader, 'native raw result adapter must exist')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'model.cst'
        self.project.write_text('synthetic placeholder, never opened by CST')

    def export(self, fake, **kwargs):
        return self.reader.export_raw_results(self.project, self.root / 'raw',
                                             results_module=fake, **kwargs)


class RawResultsTests(ResultsTestBase):
    def test_actual_per_leaf_runs_and_original_complex_data_are_exported(self):
        path = r'custom\Floquet coefficient SZmax(7),Zmax(9)'
        other = r'custom\zero dimensional'
        item = FakeItem(path, 3, [1.0, 2.0], [1+2j, 3-4j], ref=[50+1j, 51-2j])
        item0 = FakeItem(other, 0, None, 2.5)
        fake = FakeResults({path: {3: item}, other: {0: item0}})
        raw = self.export(fake)
        self.assertEqual(raw['status'], 'completed')
        self.assertEqual(raw['validation'], 'not_run')
        self.assertEqual(fake.calls[0], ('ProjectFile', str(self.project.resolve()), False))
        self.assertIn(('get_result_item', path, 3, True), fake.calls)
        self.assertIn(('get_result_item', other, 0, True), fake.calls)
        curve = raw['curves'][0]
        self.assertEqual(curve['y'][1], {'real': 3.0, 'imag': -4.0})
        self.assertEqual(curve['reference_impedance'][1], {'real': 51.0, 'imag': -2.0})
        self.assertEqual(curve['reference_impedance_consistency'], 'verified')
        self.assertEqual((curve['treepath'], curve['title'], curve['xlabel'], curve['ylabel']),
                         (path, item.title, item.xlabel, item.ylabel))
        self.assertEqual(curve['parameters'], {'theta': 0.0})
        self.assertEqual(raw['curves'][1]['kind'], '0D')
        self.assertEqual(raw['curves'][1]['x'], None)
        self.assertEqual(raw['curves'][1]['y'], 2.5)
        with (self.root / 'raw' / curve['data_csv']).open(encoding='utf-8', newline='') as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 2)
        self.assertEqual(float(rows[1]['y_imag']), -4)
        self.assertEqual(float(rows[1]['reference_impedance_imag']), -2)
        self.assertEqual(rows[0]['treepath'], path)
        saved = json.loads((self.root / 'raw' / 'resulttree.json').read_text())
        self.assertEqual(saved['curves'], raw['curves'])

    def test_missing_module_never_imports_sdk(self):
        original = builtins.__import__
        def block(name, *args, **kwargs):
            if name == 'cst' or name.startswith('cst.'):
                raise AssertionError('vendor SDK import attempted')
            return original(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=block):
            raw = self.reader.export_raw_results(self.project, self.root / 'raw')
        self.assertEqual(raw['status'], 'failed')
        self.assertIn('module_required', str(raw['errors']))

    def test_nonfinite_complex_and_reference_values_are_tagged_not_sanitized(self):
        item = FakeItem('nonfinite', 2, [math.nan], [complex(0, math.inf)], ref=[complex(50, math.nan)])
        raw = self.export(FakeResults({'nonfinite': {2: item}}))
        curve = raw['curves'][0]
        self.assertFalse(curve['finite'])
        self.assertEqual(curve['x'][0], {'nonfinite': 'nan'})
        self.assertEqual(curve['y'][0]['imag'], {'nonfinite': '+inf'})
        self.assertEqual(curve['reference_impedance'][0]['imag'], {'nonfinite': 'nan'})
        self.assertEqual(raw['status'], 'partial')
        text = (self.root / 'raw' / 'resulttree.json').read_text()
        self.assertNotIn('NaN', text)
        self.assertNotIn('Infinity', text)

    def test_error_after_good_curve_is_partial_and_does_not_hide_failure(self):
        item = FakeItem('first', 1, [1], [0.1])
        raw = self.export(FakeResults({'first': {1: item}, 'missing': {4: ValueError('no result')}}))
        self.assertEqual(raw['status'], 'partial')
        self.assertEqual(len(raw['curves']), 1)
        self.assertEqual(raw['errors'][0]['treepath'], 'missing')

    def test_run_and_parameter_mismatch_are_retained_but_not_successful(self):
        item = FakeItem('identity', 8, [1], [0.1], parameters={'theta': 10})
        raw = self.export(FakeResults({'identity': {3: item}}))
        self.assertEqual(raw['status'], 'partial')
        self.assertEqual(raw['curves'][0]['requested_run_id'], 3)
        self.assertEqual(raw['curves'][0]['run_id'], 8)
        self.assertFalse(raw['curves'][0]['identity_valid'])
        self.assertEqual(raw['curves'][0]['module_parameters'], {'theta': 0.0})

    def test_mismatched_axes_do_not_zip_truncate(self):
        item = FakeItem('bad shape', 1, [1, 2], [0.1])
        raw = self.export(FakeResults({'bad shape': {1: item}}))
        self.assertEqual(raw['status'], 'partial')
        self.assertEqual(raw['curves'][0]['x'], [1, 2])
        self.assertEqual(raw['curves'][0]['y'], [0.1])
        self.assertFalse(raw['curves'][0]['analysis_eligible'])
        self.assertIn('length', str(raw['errors']).lower())

    def test_limits_are_cumulative_across_actual_runs(self):
        first = FakeItem('a', 0, [1, 2], [0.1, 0.2])
        second = FakeItem('a', 7, [1, 2], [0.3, 0.4])
        raw = self.export(FakeResults({'a': {0: first, 7: second}}), max_total_points=3)
        self.assertEqual(raw['status'], 'partial')
        self.assertEqual(raw['counts']['points'], 2)
        self.assertIn('limit', str(raw['errors']).lower())

    def test_empty_tree_or_empty_run_list_are_failed(self):
        raw = self.export(FakeResults({}))
        self.assertEqual(raw['status'], 'failed')
        with tempfile.TemporaryDirectory() as directory:
            raw = self.reader.export_raw_results(self.project, directory,
                                                results_module=FakeResults({'empty': {}}))
            self.assertEqual(raw['status'], 'failed')

    def test_cancellation_before_open_does_not_call_projectfile(self):
        event = threading.Event()
        event.set()
        fake = FakeResults({})
        raw = self.export(fake, stop_event=event)
        self.assertEqual(raw['status'], 'cancelled')
        self.assertEqual(fake.calls, [])

    def test_cancellation_during_last_result_does_not_claim_completion(self):
        event = threading.Event()
        item = FakeItem('last', 3, [1], [0.1])
        original = item.get_ydata
        def interrupt():
            event.set()
            return original()
        item.get_ydata = interrupt
        raw = self.export(FakeResults({'last': {3: item}}), stop_event=event)
        self.assertEqual(raw['status'], 'cancelled')

    def test_conflicting_raw_data_and_axes_are_not_silently_discarded(self):
        item = FakeItem('contradiction', 3, [1, 2], [0.1, 0.2])
        item.get_data = lambda: [(1, 0.9), (2, 0.8)]
        raw = self.export(FakeResults({'contradiction': {3: item}}))
        self.assertEqual(raw['status'], 'partial')
        self.assertIn('disagree', str(raw['errors']))
        curve = raw['curves'][0]
        self.assertEqual(curve['x'], [1, 2])
        self.assertEqual(curve['y'], [0.1, 0.2])
        self.assertEqual(curve['data_tuples_raw'], [[1, 0.9], [2, 0.8]])
        self.assertFalse(curve['data_consistent'])
        self.assertFalse(curve['analysis_eligible'])
        self.assertEqual((curve['requested_run_id'], curve['run_id'], curve['treepath']), (3, 3, 'contradiction'))
        with (self.root / 'raw' / curve['data_csv']).open(newline='') as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(float(rows[0]['data_y_real']), 0.9)
        self.assertEqual(float(rows[0]['y_real']), 0.1)
        self.assertEqual(rows[0]['analysis_eligible'], 'False')

    def test_x_data_conflict_cannot_be_reset_by_consistent_reference(self):
        item = FakeItem('x contradiction', 3, [1, 2], [0.1, 0.2], ref=[50+0j, 50+0j])
        item.get_data = lambda: [(10, 0.1, 50+0j), (20, 0.2, 50+0j)]
        raw = self.export(FakeResults({'x contradiction': {3: item}}))
        curve = raw['curves'][0]
        self.assertEqual(raw['status'], 'partial')
        self.assertEqual(curve['data_tuples_raw'][0][0], 10)
        self.assertEqual(curve['x'][0], 1)
        self.assertEqual(curve['reference_impedance_consistency'], 'verified')
        self.assertFalse(curve['data_consistent'])

    def test_reported_run_identity_must_have_documented_integer_type(self):
        item = FakeItem('float identity', 3.0, [1], [0.1])
        raw = self.export(FakeResults({'float identity': {3: item}}))
        self.assertEqual(raw['status'], 'partial')
        self.assertFalse(raw['curves'][0]['identity_valid'])

    def test_curves_limit_exact_boundary_and_remaining_run_are_distinguished(self):
        item = FakeItem('limit', 3, [1], [0.1])
        raw = self.export(FakeResults({'limit': {3: item}}), max_curves=1, max_total_points=1)
        self.assertEqual(raw['status'], 'completed')
        with tempfile.TemporaryDirectory() as directory:
            raw = self.reader.export_raw_results(self.project, directory,
                results_module=FakeResults({'limit': {3: item, 8: FakeItem('limit', 8, [1], [0.2])}}), max_curves=1)
            self.assertEqual(raw['status'], 'partial')

    def test_existing_raw_evidence_is_never_mixed_with_another_export(self):
        fake = FakeResults({'one': {3: FakeItem('one', 3, [1], [0.1])}})
        self.export(fake)
        original = (self.root / 'raw' / 'resulttree.json').read_bytes()
        with self.assertRaises(FileExistsError):
            self.export(fake)
        self.assertEqual((self.root / 'raw' / 'resulttree.json').read_bytes(), original)

    def test_zero_dimensional_documented_scalar_is_authoritative_and_getter_is_retained(self):
        item = FakeItem('0D conflict', 3, None, 2.5)
        item.get_data = lambda: 999.0
        raw = self.export(FakeResults({'0D conflict': {3: item}}))
        self.assertEqual(raw['status'], 'partial')
        self.assertEqual(raw['curves'][0]['y'], 999)
        self.assertEqual(raw['curves'][0]['ydata_raw'], 2.5)

    def test_zero_dimensional_single_value_getter_array_is_not_assumed_scalar(self):
        item = FakeItem('0D array', 3, None, 2.5)
        item.get_ydata = lambda: [2.5]
        raw = self.export(FakeResults({'0D array': {3: item}}))
        self.assertEqual(raw['status'], 'completed')
        self.assertEqual(raw['curves'][0]['y'], 2.5)
        self.assertEqual(raw['curves'][0]['ydata_raw'], [2.5])

    def test_conflicting_reference_sources_are_retained_and_invalidate_export(self):
        item = FakeItem('ref conflict', 3, [1], [0.1], ref=[50+0j])
        item.get_ref_imp_data = lambda: [75+0j]
        raw = self.export(FakeResults({'ref conflict': {3: item}}))
        self.assertEqual(raw['status'], 'partial')
        self.assertEqual(raw['curves'][0]['reference_impedance'][0]['real'], 75)
        self.assertEqual(raw['curves'][0]['row_reference_impedance'][0]['real'], 50)

    def test_boolean_parameter_does_not_equal_numeric_parameter(self):
        item = FakeItem('typed parameters', 3, [1], [0.1], parameters={'theta': False})
        raw = self.export(FakeResults({'typed parameters': {3: item}}))
        self.assertEqual(raw['status'], 'partial')
        self.assertFalse(raw['curves'][0]['identity_valid'])

    def test_reference_impedance_getter_is_read_once(self):
        item = FakeItem('stable reference', 3, [1], [0.1], ref=[50+0j])
        calls = []
        def read_once():
            calls.append(1)
            if len(calls) > 1:
                raise ValueError('a repeated getter is not the same snapshot')
            return item.ref
        item.get_ref_imp_data = read_once
        raw = self.export(FakeResults({'stable reference': {3: item}}))
        self.assertEqual(raw['status'], 'completed')
        self.assertEqual(len(calls), 1)

    def test_equivalent_real_reference_and_complex_tuple_do_not_conflict(self):
        item = FakeItem('same reference', 3, [1], [0.1], ref=[50+0j])
        item.get_ref_imp_data = lambda: [50.0]
        raw = self.export(FakeResults({'same reference': {3: item}}))
        self.assertEqual(raw['status'], 'completed')
        self.assertEqual(raw['curves'][0]['reference_impedance_consistency'], 'verified')

    def test_actual_overlimit_snapshots_retain_both_sources_as_invalid_raw(self):
        item = FakeItem('limited conflict', 3, [1, 2], [0.1, 0.2])
        item.length = 1
        item.get_data = lambda: [(1, 0.9), (2, 0.8)]
        raw = self.export(FakeResults({'limited conflict': {3: item}}), max_total_points=1)
        self.assertEqual(raw['status'], 'partial')
        curve = raw['curves'][0]
        self.assertEqual(curve['x'], [1, 2])
        self.assertEqual(curve['y'], [0.1, 0.2])
        self.assertEqual(curve['data_tuples_raw'], [[1, 0.9], [2, 0.8]])
        self.assertEqual(curve['run_id'], 3)
        self.assertFalse(curve['analysis_eligible'])
        self.assertTrue(any('limit' in reason for reason in curve['invalid_reasons']))

    def test_long_numeric_sources_retain_bounded_prefix_and_truncation_flags(self):
        item = FakeItem('truncated conflict', 3, [1, 2, 3, 4], [0.1, 0.2, 0.3, 0.4])
        item.length = 1
        item.get_data = lambda: [(1, 0.9), (2, 0.8), (3, 0.7), (4, 0.6)]
        raw = self.export(FakeResults({'truncated conflict': {3: item}}), max_total_points=1)
        self.assertEqual(raw['status'], 'partial')
        curve = raw['curves'][0]
        self.assertEqual(curve['x'], [1, 2])
        self.assertEqual(curve['y'], [0.1, 0.2])
        self.assertEqual(curve['data_tuples_raw'], [[1, 0.9], [2, 0.8]])
        self.assertEqual(curve['snapshot_truncated'], {'x': True, 'y': True, 'data': True, 'reference_impedance': False})
        self.assertFalse(curve['analysis_eligible'])

    def test_bounded_unknown_iterable_does_not_consume_unretained_numeric_sample(self):
        item = FakeItem('bounded generator', 3, [1, 2, 3], [0.1, 0.2, 0.3])
        item.length = 1
        consumed = []
        def x_generator():
            for value in item.x:
                consumed.append(value)
                yield value
        item.get_xdata = x_generator
        raw = self.export(FakeResults({'bounded generator': {3: item}}), max_total_points=1)
        self.assertEqual(consumed, [1, 2])
        self.assertEqual(raw['curves'][0]['x'], consumed)
        self.assertTrue(raw['curves'][0]['snapshot_truncated']['x'])
        self.assertFalse(raw['curves'][0]['analysis_eligible'])

    def test_reference_overlimit_retains_numeric_and_reference_snapshots(self):
        item = FakeItem('reference limit', 3, [1], [0.1], ref=[50])
        item.get_ref_imp_data = lambda: [75, 76, 77, 78]
        raw = self.export(FakeResults({'reference limit': {3: item}}), max_total_points=1)
        self.assertEqual(raw['status'], 'partial')
        curve = raw['curves'][0]
        self.assertEqual(curve['x'], [1])
        self.assertEqual(curve['y'], [0.1])
        self.assertEqual(curve['data_tuples_raw'], [[1, 0.1, 50]])
        self.assertEqual(curve['reference_impedance'], [75, 76])
        self.assertTrue(curve['snapshot_truncated']['reference_impedance'])
        self.assertFalse(curve['analysis_eligible'])

    def test_scalar_empty_or_missing_reference_cannot_verify_tuple_impedance(self):
        for index, ref in enumerate((75, [], None)):
            item = FakeItem('unknown reference', 3, [1], [0.1], ref=[50])
            item.get_ref_imp_data = lambda ref=ref: ref
            raw = self.reader.export_raw_results(self.project, self.root / str(index),
                results_module=FakeResults({'unknown reference': {3: item}}))
            self.assertEqual(raw['status'], 'partial')
            self.assertEqual(raw['curves'][0]['reference_impedance'], ref)
            self.assertEqual(raw['curves'][0]['row_reference_impedance'], [50])
            self.assertFalse(raw['curves'][0]['analysis_eligible'])
            self.assertTrue(any('reference' in reason for reason in raw['curves'][0]['invalid_reasons']))


class CanonicalResultsTests(ResultsTestBase):
    def fixture(self):
        items = {}
        def curve(path, y, ylabel):
            item = FakeItem(path, 3, [1, 2], y)
            item.ylabel = ylabel
            item.title = path.rsplit('\\', 1)[-1]
            items[path] = {3: item}
            return {'treepath': path, 'run_id': 3, 'title': item.title,
                    'xlabel': item.xlabel, 'ylabel': ylabel,
                    'x_unit': 'GHz', 'x_to_Hz': 1e9,
                    'y_unit': ylabel.rsplit(' / ', 1)[-1], 'y_scale': 1}
        te_s = curve(r'arbitrary\coefficient 7', [0.1+0.2j, 0.3+0.4j], 'Coefficient / 1')
        tm_s = curve(r'arbitrary\coefficient 9', [0.05j, 0.06j], 'Coefficient / 1')
        for selector, receive in ((te_s, 7), (tm_s, 9)):
            selector['title'] = items[selector['treepath']][3].title = f'SZmax({receive}),Zmax(7)'
        te_g = curve(r'arbitrary\propagation 7', [20j, 40j], 'Gamma / 1/m')
        tm_g = curve(r'arbitrary\propagation 9', [20j, 40j], 'Gamma / 1/m')
        incident = curve(r'arbitrary\Excitation [Zmax(7)]\Power Stimulated', [2, 3], 'Power / W')
        reflected = curve(r'arbitrary\Excitation [Zmax(7)]\Power Outgoing all Ports', [0.2, 0.3], 'Power / W')
        loss_a = curve(r'arbitrary\Excitation [Zmax(7)]\Loss per Material\a', [0.4, 0.5], 'Power / W')
        loss_b = curve(r'arbitrary\Excitation [Zmax(7)]\Loss per Material\b', [1.4, 2.2], 'Power / W')
        eps = curve(r'unknown fitted curves\epsilon a', [2-0.1j, 3-0.2j], 'Permittivity / 1')
        mu = curve(r'unknown fitted curves\mu a', [1-0.05j, 1-0.06j], 'Permeability / 1')
        eps_b = curve(r'unknown fitted curves\epsilon b', [4-0.3j, 5-0.4j], 'Permittivity / 1')
        mu_b = curve(r'unknown fitted curves\mu b', [1-0.01j, 1-0.02j], 'Permeability / 1')
        raw = self.export(FakeResults(items))
        case = {'signature': 'synthetic-signature', 'materials': {'a': {}, 'b': {}},
                'scenario': {'frequencies_Hz': [1e9, 2e9], 'profile': 'periodic-pec', 'polarization': 'TE'},
                'material_samples': {
                    'a': {'frequencies_Hz': [1e9, 2e9], 'epsilon_real': [2, 3],
                          'epsilon_imag': [-0.1, -0.2], 'mu_real': [1, 1], 'mu_imag': [-0.05, -0.06]},
                    'b': {'frequencies_Hz': [1e9, 2e9], 'epsilon_real': [4, 5],
                          'epsilon_imag': [-0.3, -0.4], 'mu_real': [1, 1], 'mu_imag': [-0.01, -0.02]}}}
        modes = [{'port': 'Zmax', 'index': 7, 'name': 'TE(0,0)', 'm': 0, 'n': 0, 'polarization': 'TE'},
                 {'port': 'Zmax', 'index': 9, 'name': 'TM(0,0)', 'm': 0, 'n': 0, 'polarization': 'TM'}]
        model = {'status': 'model_report_validated', 'modes_port': 'Zmax', 'modes': modes,
                 'boundaries': {'Zmin': 'electric', 'Zmax': 'open'}}
        profile = {'schema_version': 'cst-result-profile/1', 'confirmed': True,
                   'case_signature': case['signature'], 'run_id': 3, 'expected_parameters': {'theta': 0.0},
                   'normalization': 'power', 'normalization_evidence': 'Synthetic declared normalization source',
                   'excitation': {
                       'incident_mode': {'port': 'Zmax', 'native_mode_index': 7, 'mode_name': 'TE(0,0)',
                                         'm': 0, 'n': 0, 'polarization': 'TE'},
                       's_identity': {'field': 'title',
                                      'template': 'S{receive_port}({receive_mode}),{incident_port}({incident_mode})'},
                       'power_identity': {'field': 'treepath',
                                          'template': r'arbitrary\Excitation [{incident_port}({incident_mode})]'}},
                   'modes': [dict(port='Zmax', native_mode_index=7, mode_name='TE(0,0)', m=0, n=0,
                                  polarization='TE', reflection=te_s, gamma=te_g),
                             dict(port='Zmax', native_mode_index=9, mode_name='TM(0,0)', m=0, n=0,
                                  polarization='TM', reflection=tm_s, gamma=tm_g)],
                   'power': {'stimulated': incident, 'reflected': reflected,
                             'material_absorbed': [{'material_id': 'a', 'selector': loss_a},
                                                   {'material_id': 'b', 'selector': loss_b}]},
                   'boundary_assumption': {'kind': 'PEC', 'boundary': 'Zmin', 'value': 'electric'},
                   'materials': [{'material_id': name, 'role': 'fitted_response',
                                  'source_time_convention': 'exp(+jωt)', 'epsilon': epsilon, 'mu': permeability,
                                  'relative_tolerance': 1e-6, 'absolute_tolerance': 1e-9}
                                 for name, epsilon, permeability in [('a', eps, mu), ('b', eps_b, mu_b)]]}
        return raw, profile, case, model

    def convert(self, raw, profile, case, model, name='canonical'):
        self.assertTrue(hasattr(self.reader, 'canonicalize_raw_results'), 'explicit profile converter must exist')
        return self.reader.canonicalize_raw_results(raw, profile, case, model, self.root / name)

    def test_exact_profile_exports_measured_powers_and_original_gamma(self):
        raw, profile, case, model = self.fixture()
        result = self.convert(raw, profile, case, model)
        self.assertEqual(result['status'], 'diagnostic_only')
        self.assertFalse(result['physical_accepted'])
        self.assertFalse(result['numerically_qualified'])
        self.assertEqual(result['native_acceptance'], 'not_run')
        self.assertEqual(result['mapping_evidence'], 'profile_declared_not_native_accepted')
        with Path(result['spectra_path']).open(newline='') as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 4)
        self.assertEqual(float(rows[0]['gamma_im_per_m']), 20)
        self.assertEqual(float(rows[0]['s_im']), 0.2)
        with Path(result['power_path']).open(newline='') as handle:
            power = list(csv.DictReader(handle))
        self.assertEqual(float(power[0]['incident_W']), 2)
        self.assertEqual(float(power[0]['reflected_W']), 0.2)
        self.assertAlmostEqual(float(power[0]['absorbed_W']), 1.8)
        self.assertEqual(float(power[0]['transmitted_W']), 0)
        self.assertEqual(result['transmission_source'], 'read_back_PEC_boundary_assumption')
        self.assertEqual(result['material_fit_readback']['status'], 'profile_declared_fit_matches_samples')

    def test_profile_labels_units_factors_and_parameters_must_match(self):
        raw, profile, case, model = self.fixture()
        mutations = [('label', lambda p: p['modes'][0]['reflection'].update(xlabel='Frequency / MHz')),
                     ('factor', lambda p: p['modes'][0]['reflection'].update(x_to_Hz=1)),
                     ('gammaunit', lambda p: p['modes'][0]['gamma'].update(y_unit='unknown')),
                     ('run', lambda p: p['modes'][0]['gamma'].update(run_id=0)),
                     ('params', lambda p: p.update(expected_parameters={'theta': 1})),
                     ('empty_evidence', lambda p: p.update(normalization_evidence=' '))]
        for name, mutation in mutations:
            with self.subTest(name=name):
                invalid = copy.deepcopy(profile)
                mutation(invalid)
                result = self.convert(raw, invalid, case, model, name)
                self.assertEqual(result['status'], 'failed')
                self.assertIsNone(result['spectra_path'])

    def test_native_mode_index_must_match_actual_readback(self):
        raw, profile, case, model = self.fixture()
        model['modes'][0]['index'] = 1
        self.assertEqual(self.convert(raw, profile, case, model)['status'], 'failed')

    def test_duplicate_source_cannot_supply_two_modes_or_two_materials(self):
        raw, profile, case, model = self.fixture()
        profile['modes'][1]['reflection'] = copy.deepcopy(profile['modes'][0]['reflection'])
        self.assertEqual(self.convert(raw, profile, case, model)['status'], 'failed')
        profile['modes'][1]['reflection'] = self.fixture_profile_selector(raw, r'arbitrary\coefficient 9')
        profile['power']['material_absorbed'][1]['selector'] = profile['power']['material_absorbed'][0]['selector']
        self.assertEqual(self.convert(raw, profile, case, model, 'duplicate_loss')['status'], 'failed')

    @staticmethod
    def fixture_profile_selector(raw, path):
        curve = next(curve for curve in raw['curves'] if curve['treepath'] == path)
        return {**{key: curve[key] for key in ('treepath', 'run_id', 'title', 'xlabel', 'ylabel')},
                'x_unit': 'GHz', 'x_to_Hz': 1e9, 'y_unit': '1', 'y_scale': 1}

    def test_accepted_and_port_absorption_are_forbidden_as_material_loss(self):
        raw, profile, case, model = self.fixture()
        for index, title in enumerate(('Power Accepted', 'Power Absorbed at all Ports')):
            invalid_raw, invalid_profile = copy.deepcopy(raw), copy.deepcopy(profile)
            source = invalid_profile['power']['material_absorbed'][0]['selector']
            curve = next(curve for curve in invalid_raw['curves'] if curve['treepath'] == source['treepath'])
            source['title'] = curve['title'] = title
            self.assertEqual(self.convert(invalid_raw, invalid_profile, case, model, str(index))['status'], 'failed')

    def test_material_coverage_frequency_and_pec_boundary_are_required(self):
        raw, profile, case, model = self.fixture()
        invalid = copy.deepcopy(profile)
        invalid['power']['material_absorbed'].pop()
        self.assertEqual(self.convert(raw, invalid, case, model, 'missing_material')['status'], 'failed')
        invalid_raw = copy.deepcopy(raw)
        invalid_raw['curves'][7]['x'] = [1, 2.01]
        self.assertEqual(self.convert(invalid_raw, profile, case, model, 'freq')['status'], 'failed')
        model['boundaries']['Zmin'] = 'open'
        self.assertEqual(self.convert(raw, profile, case, model, 'boundary')['status'], 'failed')

    def test_partial_raw_is_not_canonicalized(self):
        raw, profile, case, model = self.fixture()
        raw['status'] = 'partial'
        self.assertEqual(self.convert(raw, profile, case, model)['status'], 'failed')

    def test_gamma_per_mm_requires_declared_confirmed_factor(self):
        raw, profile, case, model = self.fixture()
        source = profile['modes'][0]['gamma']
        curve = next(curve for curve in raw['curves'] if curve['treepath'] == source['treepath'])
        source.update(ylabel='Gamma / 1/mm', y_unit='1/mm', y_scale=1000)
        curve['ylabel'] = source['ylabel']
        curve['y'] = [{'real': 0, 'imag': 0.02}, {'real': 0, 'imag': 0.04}]
        result = self.convert(raw, profile, case, model)
        self.assertEqual(result['status'], 'diagnostic_only')
        with Path(result['spectra_path']).open(newline='') as handle:
            self.assertEqual(float(next(csv.DictReader(handle))['gamma_im_per_m']), 20)

    def test_missing_fit_mapping_remains_unresolved(self):
        raw, profile, case, model = self.fixture()
        profile.pop('materials')
        result = self.convert(raw, profile, case, model)
        self.assertEqual(result['status'], 'diagnostic_only')
        self.assertIn('material_fit_readback', result['unresolved_gates'])
        self.assertEqual(result['material_fit_readback']['status'], 'unresolved')

    def test_wrong_fit_is_rejected_with_numeric_error_report(self):
        raw, profile, case, model = self.fixture()
        curve = next(curve for curve in raw['curves'] if curve['treepath'] == profile['materials'][0]['epsilon']['treepath'])
        curve['y'][0]['real'] = 20
        result = self.convert(raw, profile, case, model)
        self.assertEqual(result['status'], 'failed')
        self.assertGreater(result['material_fit_readback']['materials'][0]['max_absolute_error'], 1)

    def test_fit_unit_and_original_role_cannot_claim_fit_match(self):
        raw, profile, case, model = self.fixture()
        invalid = copy.deepcopy(profile)
        invalid['materials'][0]['role'] = 'original_response'
        self.assertEqual(self.convert(raw, invalid, case, model, 'original')['status'], 'failed')
        invalid = copy.deepcopy(profile)
        invalid['materials'][0]['epsilon']['y_unit'] = 'F/m'
        self.assertEqual(self.convert(raw, invalid, case, model, 'fit_unit')['status'], 'failed')

    def test_declared_opposite_material_convention_is_conjugated_explicitly(self):
        raw, profile, case, model = self.fixture()
        profile['materials'][0]['source_time_convention'] = 'exp(-jωt)'
        paths = [profile['materials'][0][component]['treepath'] for component in ('epsilon', 'mu')]
        for curve in raw['curves']:
            if curve['treepath'] in paths:
                for value in curve['y']:
                    value['imag'] = -value['imag']
        result = self.convert(raw, profile, case, model)
        self.assertEqual(result['status'], 'diagnostic_only')
        self.assertEqual(result['material_fit_readback']['source_role'], 'profile_declared_fitted_response')
        self.assertEqual(result['material_fit_readback']['native_acceptance'], 'pending')

    def test_boolean_actual_parameter_does_not_match_numeric_profile(self):
        raw, profile, case, model = self.fixture()
        for curve in raw['curves']:
            curve['parameters'] = curve['module_parameters'] = {'theta': False}
        self.assertEqual(self.convert(raw, profile, case, model)['status'], 'failed')

    def test_failed_second_fit_component_does_not_mark_material_matched(self):
        raw, profile, case, model = self.fixture()
        profile['materials'][0]['mu']['y_unit'] = 'unknown'
        result = self.convert(raw, profile, case, model)
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(result['material_fit_readback']['materials'][0]['matched'])

    @staticmethod
    def set_actual_incidence(raw, profile, case, polarization, index):
        case['scenario']['polarization'] = polarization
        mode = next(mode for mode in profile['modes'] if mode['polarization'] == polarization)
        profile['excitation']['incident_mode'] = {key: mode[key] for key in
            ('port', 'native_mode_index', 'mode_name', 'm', 'n', 'polarization')}
        for mode in profile['modes']:
            selector = mode['reflection']
            curve = next(curve for curve in raw['curves'] if curve['treepath'] == selector['treepath'])
            curve['title'] = selector['title'] = f"S{mode['port']}({mode['native_mode_index']}),Zmax({index})"
        selectors = [profile['power']['stimulated'], profile['power']['reflected'],
                     *[entry['selector'] for entry in profile['power']['material_absorbed']]]
        for selector in selectors:
            curve = next(curve for curve in raw['curves'] if curve['treepath'] == selector['treepath'])
            new_path = selector['treepath'].replace('[Zmax(7)]', f'[Zmax({index})]')
            curve['treepath'] = curve['reported_treepath'] = selector['treepath'] = new_path

    def assert_identity_failure(self, result, directory='canonical'):
        self.assertEqual(result['status'], 'failed')
        self.assertIsNone(result['spectra_path'])
        self.assertIsNone(result['power_path'])
        self.assertFalse((self.root / directory / 'spectra.csv').exists())
        self.assertFalse((self.root / directory / 'power.csv').exists())

    def test_tm_case_cannot_analyze_whole_te_incident_column_and_power_branch(self):
        raw, profile, case, model = self.fixture()
        case['scenario']['polarization'] = 'TM'
        profile['excitation']['incident_mode'] = {key: profile['modes'][1][key] for key in
            ('port', 'native_mode_index', 'mode_name', 'm', 'n', 'polarization')}
        result = self.convert(raw, profile, case, model)
        self.assert_identity_failure(result)
        self.assertIn('incident', str(result['errors']).lower())

    def test_te_case_cannot_analyze_whole_tm_incident_column(self):
        raw, profile, case, model = self.fixture()
        self.set_actual_incidence(raw, profile, case, 'TM', 9)
        case['scenario']['polarization'] = 'TE'
        profile['excitation']['incident_mode'] = {key: profile['modes'][0][key] for key in
            ('port', 'native_mode_index', 'mode_name', 'm', 'n', 'polarization')}
        self.assert_identity_failure(self.convert(raw, profile, case, model))

    def test_correct_tm_incidence_is_bound_to_actual_labels_and_model(self):
        raw, profile, case, model = self.fixture()
        self.set_actual_incidence(raw, profile, case, 'TM', 9)
        result = self.convert(raw, profile, case, model)
        self.assertEqual(result['status'], 'diagnostic_only')
        identity = result['excitation_identity']
        self.assertEqual(identity['status'], 'validated_against_actual_labels')
        self.assertEqual(identity['incident_mode']['native_mode_index'], 9)
        self.assertEqual(identity['incident_mode']['polarization'], 'TM')
        self.assertEqual(len(identity['s_columns']), 2)
        self.assertEqual(len(identity['power_branches']), 4)
        self.assertTrue(all(row['incident_mode'] == 9 for row in identity['s_columns']))
        self.assertTrue(all('[Zmax(9)]' in row['branch'] for row in identity['power_branches']))

    def test_one_s_incident_column_or_receiving_mode_cannot_be_swapped(self):
        raw, profile, case, model = self.fixture()
        for name, title in [('column', 'SZmax(9),Zmax(9)'), ('receiving', 'SZmax(7),Zmax(7)'),
                            ('port', 'SZmax(9),Zmin(7)')]:
            with self.subTest(name=name):
                r, p = copy.deepcopy(raw), copy.deepcopy(profile)
                selector = p['modes'][1]['reflection']
                curve = next(curve for curve in r['curves'] if curve['treepath'] == selector['treepath'])
                selector['title'] = curve['title'] = title
                self.assert_identity_failure(self.convert(r, p, case, model, name), name)

    def test_actual_power_excitation_branch_must_match_s_incidence(self):
        raw, profile, case, model = self.fixture()
        selector = profile['power']['material_absorbed'][1]['selector']
        curve = next(curve for curve in raw['curves'] if curve['treepath'] == selector['treepath'])
        new_path = selector['treepath'].replace('[Zmax(7)]', '[Zmax(9)]')
        curve['treepath'] = curve['reported_treepath'] = selector['treepath'] = new_path
        result = self.convert(raw, profile, case, model)
        self.assert_identity_failure(result)
        self.assertIn('excitation', str(result['errors']).lower())

    def test_power_branch_prefix_separator_and_port_are_exact(self):
        raw, profile, case, model = self.fixture()
        original = profile['power']['stimulated']['treepath']
        values = [original.replace('(7)', '(70)'), original.replace(']\\', ']extra\\'),
                  'unrelated\\' + original, r'arbitrary\Excitation [Zmax(7)]',
                  original.replace('Zmax(7)', 'Zmin(7)')]
        for index, new_path in enumerate(values):
            r, p = copy.deepcopy(raw), copy.deepcopy(profile)
            selector = p['power']['stimulated']
            curve = next(curve for curve in r['curves'] if curve['treepath'] == original)
            curve['treepath'] = curve['reported_treepath'] = selector['treepath'] = new_path
            self.assert_identity_failure(self.convert(r, p, case, model, str(index)), str(index))

    def test_missing_legacy_or_unparseable_identity_has_specific_unresolved_gate(self):
        raw, profile, case, model = self.fixture()
        p = copy.deepcopy(profile)
        p.pop('excitation')
        result = self.convert(raw, p, case, model, 'legacy')
        self.assert_identity_failure(result, 'legacy')
        self.assertIn('incident_excitation_identity', result['unresolved_gates'])
        for key, gate in [('s_identity', 's_matrix_incident_column'), ('power_identity', 'power_excitation_identity')]:
            p = copy.deepcopy(profile)
            p['excitation'].pop(key)
            result = self.convert(raw, p, case, model, key)
            self.assert_identity_failure(result, key)
            self.assertIn(gate, result['unresolved_gates'])

    def test_unparseable_actual_s_label_cannot_be_replaced_by_declared_incident(self):
        raw, profile, case, model = self.fixture()
        selector = profile['modes'][0]['reflection']
        curve = next(curve for curve in raw['curves'] if curve['treepath'] == selector['treepath'])
        selector['title'] = curve['title'] = 'coefficient without matrix column'
        result = self.convert(raw, profile, case, model)
        self.assert_identity_failure(result)
        self.assertIn('s_matrix_incident_column', result['unresolved_gates'])

    def test_template_cannot_skip_or_repeat_identity_slots_or_use_format_syntax(self):
        raw, profile, case, model = self.fixture()
        templates = ['SZmax(7),Zmax(7)', 'S{receive_port}({receive_mode}),Zmax(7)',
                     'S{receive_port}({receive_mode}),{incident_port}({incident_mode}){incident_mode}',
                     'S{receive_port}({receive_mode}),{incident_port}({incident_mode:03d})',
                     'S{receive_port}({receive_mode}),{incident_port}({incident_mode:})',
                     'S{receive_port}({receive_mode}),{incident_port!s}({incident_mode})',
                     '{receive_port}{receive_mode}{incident_port}{incident_mode}']
        for index, template in enumerate(templates):
            p = copy.deepcopy(profile)
            p['excitation']['s_identity']['template'] = template
            self.assert_identity_failure(self.convert(raw, p, case, model, str(index)), str(index))

    def test_conflicting_native_mode_key_and_nonfundamental_incidence_are_rejected(self):
        raw, profile, case, model = self.fixture()
        m, p = copy.deepcopy(model), copy.deepcopy(profile)
        m['modes'][1]['index'] = p['modes'][1]['native_mode_index'] = 7
        result = self.convert(raw, p, case, m, 'key')
        self.assert_identity_failure(result, 'key')
        self.assertIn('native mode key', str(result['errors']).lower())
        m, p = copy.deepcopy(model), copy.deepcopy(profile)
        m['modes'][0]['m'] = p['modes'][0]['m'] = p['excitation']['incident_mode']['m'] = 1
        self.assert_identity_failure(self.convert(raw, p, case, m, 'order'), 'order')

    def test_invalid_raw_numeric_snapshot_is_not_eligible_even_if_status_is_tampered(self):
        raw, profile, case, model = self.fixture()
        raw['curves'][0]['analysis_eligible'] = False
        raw['curves'][0]['data_consistent'] = False
        raw['curves'][0]['invalid_reasons'] = ['raw tuple/getter disagree']
        self.assert_identity_failure(self.convert(raw, profile, case, model))

    def test_explicit_generic_tree_prefix_uses_actual_source_fields(self):
        raw, profile, case, model = self.fixture()
        profile['excitation']['s_identity'] = {
            'field': 'treepath',
            'template': 'dump/custom/scattering/S{receive_port}({receive_mode}),{incident_port}({incident_mode})'}
        profile['excitation']['power_identity'] = {
            'field': 'treepath', 'template': 'study/custom/power/Excitation [{incident_port}({incident_mode})]'}
        for mode in profile['modes']:
            selector = mode['reflection']
            curve = next(curve for curve in raw['curves'] if curve['treepath'] == selector['treepath'])
            path = f"dump/custom/scattering/S{mode['port']}({mode['native_mode_index']}),Zmax(7)"
            curve['treepath'] = curve['reported_treepath'] = selector['treepath'] = path
        for selector in [profile['power']['stimulated'], profile['power']['reflected'],
                         *[entry['selector'] for entry in profile['power']['material_absorbed']]]:
            curve = next(curve for curve in raw['curves'] if curve['treepath'] == selector['treepath'])
            quantity = selector['treepath'].split(']\\', 1)[1].replace('\\', '/')
            path = 'study/custom/power/Excitation [Zmax(7)]/' + quantity
            curve['treepath'] = curve['reported_treepath'] = selector['treepath'] = path
        result = self.convert(raw, profile, case, model)
        self.assertEqual(result['status'], 'diagnostic_only')
        self.assertTrue(all(row['source_field'] == 'treepath' for row in result['excitation_identity']['s_columns']))
        self.assertTrue(all(row['branch'] == 'study/custom/power/Excitation [Zmax(7)]'
                            for row in result['excitation_identity']['power_branches']))

    def test_identity_slots_cannot_slice_numeric_or_port_tokens(self):
        raw, profile, case, model = self.fixture()
        for name, suffix in [('mode_suffix', '{incident_mode}0'),
                             ('mode_prefix', '10{incident_mode}'),
                             ('receive_suffix', '{receive_mode}0'),
                             ('port_suffix', '{incident_port}.extra')]:
            r, p = copy.deepcopy(raw), copy.deepcopy(profile)
            template = p['excitation']['s_identity']['template']
            slot = '{receive_mode}' if name == 'receive_suffix' else ('{incident_port}' if name == 'port_suffix' else '{incident_mode}')
            p['excitation']['s_identity']['template'] = template.replace(slot, suffix)
            for mode in p['modes']:
                selector = mode['reflection']
                curve = next(c for c in r['curves'] if c['treepath'] == selector['treepath'])
                values = {'receive_port': 'Zmax', 'receive_mode': mode['native_mode_index'],
                          'incident_port': 'Zmax', 'incident_mode': 7}
                curve['title'] = selector['title'] = p['excitation']['s_identity']['template'].format(**values)
            result = self.convert(r, p, case, model, name)
            self.assert_identity_failure(result, name)
            self.assertIn('s_matrix_incident_column', result['unresolved_gates'])
        for name, replacement in [('power_suffix', '{incident_mode}0'),
                                  ('power_prefix', '10{incident_mode}'),
                                  ('power_port', '{incident_port}.extra')]:
            r, p = copy.deepcopy(raw), copy.deepcopy(profile)
            slot = '{incident_port}' if name == 'power_port' else '{incident_mode}'
            rule = p['excitation']['power_identity']
            rule['template'] = rule['template'].replace(slot, replacement)
            for selector in [p['power']['stimulated'], p['power']['reflected'],
                             *[entry['selector'] for entry in p['power']['material_absorbed']]]:
                curve = next(c for c in r['curves'] if c['treepath'] == selector['treepath'])
                quantity = selector['treepath'].split(']\\', 1)[1]
                path = rule['template'].format(incident_port='Zmax', incident_mode=7) + '\\' + quantity
                curve['treepath'] = curve['reported_treepath'] = selector['treepath'] = path
            result = self.convert(r, p, case, model, name)
            self.assert_identity_failure(result, name)
            self.assertIn('power_excitation_identity', result['unresolved_gates'])

    def test_fixed_wrong_identity_cannot_be_overridden_by_appended_slots(self):
        original = self.fixture()
        for role in ('s', 'power'):
            raw, profile, case, model = copy.deepcopy(original)
            self.set_actual_incidence(raw, profile, case, 'TM', 9)
            if role == 's':
                rule = profile['excitation']['s_identity']
                rule['template'] = 'SZmax(7),Zmax(7); annotation=' + rule['template']
                for mode in profile['modes']:
                    selector = mode['reflection']
                    curve = next(c for c in raw['curves'] if c['treepath'] == selector['treepath'])
                    curve['title'] = selector['title'] = rule['template'].format(
                        receive_port='Zmax', receive_mode=mode['native_mode_index'], incident_port='Zmax', incident_mode=9)
            else:
                rule = profile['excitation']['power_identity']
                rule['template'] = r'arbitrary\Excitation [Zmax(7)]\annotation\Excitation [{incident_port}({incident_mode})]'
                for selector in [profile['power']['stimulated'], profile['power']['reflected'],
                                 *[entry['selector'] for entry in profile['power']['material_absorbed']]]:
                    curve = next(c for c in raw['curves'] if c['treepath'] == selector['treepath'])
                    quantity = selector['treepath'].split(']\\', 1)[1]
                    path = rule['template'].format(incident_port='Zmax', incident_mode=9) + '\\' + quantity
                    curve['treepath'] = curve['reported_treepath'] = selector['treepath'] = path
            result = self.convert(raw, profile, case, model, role)
            self.assert_identity_failure(result, role)
            self.assertIn('s_matrix_incident_column' if role == 's' else 'power_excitation_identity', result['unresolved_gates'])

    def test_unknown_identity_grammar_and_second_excitation_branch_remain_unresolved(self):
        raw, profile, case, model = self.fixture()
        original = copy.deepcopy((raw, profile, case, model))
        p = copy.deepcopy(profile)
        p['excitation']['s_identity']['template'] = 'from/{incident_port}({incident_mode})/to/{receive_port}({receive_mode})'
        for mode in p['modes']:
            selector = mode['reflection']
            curve = next(c for c in raw['curves'] if c['treepath'] == selector['treepath'])
            curve['title'] = selector['title'] = p['excitation']['s_identity']['template'].format(
                receive_port='Zmax', receive_mode=mode['native_mode_index'], incident_port='Zmax', incident_mode=7)
        result = self.convert(raw, p, case, model, 'unknown')
        self.assert_identity_failure(result, 'unknown')
        self.assertIn('s_matrix_incident_column', result['unresolved_gates'])
        raw, profile, case, model = original
        selector = profile['power']['stimulated']
        curve = next(c for c in raw['curves'] if c['treepath'] == selector['treepath'])
        path = selector['treepath'].replace(']\\', ']\\Excitation [Zmax(9)]\\')
        curve['treepath'] = curve['reported_treepath'] = selector['treepath'] = path
        result = self.convert(raw, profile, case, model, 'nested')
        self.assert_identity_failure(result, 'nested')
        self.assertIn('power_excitation_identity', result['unresolved_gates'])

    def test_recognizable_actual_s_title_and_tree_cannot_disagree(self):
        raw, profile, case, model = self.fixture()
        profile['excitation']['s_identity'] = {'field': 'treepath',
            'template': 'custom/S{receive_port}({receive_mode}),{incident_port}({incident_mode})'}
        for mode in profile['modes']:
            selector = mode['reflection']
            curve = next(c for c in raw['curves'] if c['treepath'] == selector['treepath'])
            path = f"custom/SZmax({mode['native_mode_index']}),Zmax(7)"
            curve['treepath'] = curve['reported_treepath'] = selector['treepath'] = path
        selector = profile['modes'][0]['reflection']
        curve = next(c for c in raw['curves'] if c['treepath'] == selector['treepath'])
        curve['title'] = selector['title'] = 'SZmax(7),Zmax(9)'
        result = self.convert(raw, profile, case, model)
        self.assert_identity_failure(result)
        self.assertIn('s_matrix_incident_column', result['unresolved_gates'])

    def test_other_actual_s_field_display_suffix_cannot_hide_wrong_column(self):
        original = self.fixture()
        for selected_field in ('treepath', 'title'):
            raw, profile, case, model = copy.deepcopy(original)
            profile['excitation']['s_identity'] = {'field': selected_field,
                'template': ('custom/' if selected_field == 'treepath' else '') +
                    'S{receive_port}({receive_mode}),{incident_port}({incident_mode})'}
            for mode in profile['modes']:
                selector = mode['reflection']
                curve = next(c for c in raw['curves'] if c['treepath'] == selector['treepath'])
                identity = f"SZmax({mode['native_mode_index']}),Zmax(7)"
                path = 'custom/' + identity
                curve['treepath'] = curve['reported_treepath'] = selector['treepath'] = path
                curve['title'] = selector['title'] = identity
            selector = profile['modes'][0]['reflection']
            curve = next(c for c in raw['curves'] if c['treepath'] == selector['treepath'])
            if selected_field == 'treepath':
                curve['title'] = selector['title'] = 'SZmax(7),Zmax(9) [Magnitude]'
            else:
                curve['treepath'] = curve['reported_treepath'] = selector['treepath'] = 'custom/SZmax(7),Zmax(9) [Magnitude]'
            result = self.convert(raw, profile, case, model, selected_field)
            self.assert_identity_failure(result, selected_field)
            self.assertIn('s_matrix_incident_column', result['unresolved_gates'])

    def split_material_fixture(self, source_role='fd_interpolated'):
        raw, profile, case, model = self.fixture()
        suffix = 'FD - Interpolated' if source_role == 'fd_interpolated' else 'Fit'
        for entry in profile['materials']:
            entry.update(role='fd_interpolated_response' if source_role == 'fd_interpolated' else 'fitted_response',
                         source_role=source_role, native_material_name='mat_' + entry['material_id'],
                         conductivity_treatment='included_in_epsilon', native_conductivity_S_m=0)
            case['material_samples'][entry['material_id']]['conductivity_S_m'] = 0
            for component, native in [('epsilon', 'Eps'), ('mu', 'Mu')]:
                original = next(c for c in raw['curves'] if c['treepath'] == entry[component]['treepath'])
                raw['curves'].remove(original)
                selectors = {}
                for field, prime in [('real', "'"), ('loss', "''")]:
                    curve = copy.deepcopy(original)
                    path = '\\'.join(('1D Results', 'Materials', entry['native_material_name'], 'Dispersive', native + prime + ' (' + suffix + ')'))
                    curve.update(treepath=path, reported_treepath=path,
                                 title=('Electric' if component == 'epsilon' else 'Magnetic') + ' Dispersion: Nth Order Model, N=1 (Fit)',
                                 ylabel='', y=[value['real'] if field == 'real' else -value['imag'] for value in original['y']])
                    raw['curves'].append(curve)
                    selectors[field] = {**{key: curve[key] for key in ('treepath', 'run_id', 'title', 'xlabel', 'ylabel')},
                                        'x_unit': 'GHz', 'x_to_Hz': 1e9, 'y_unit': '1', 'y_scale': 1}
                entry[component] = {'representation': 'real_positive_loss', 'sample_policy': 'exact_planned_subset', **selectors}
        return raw, profile, case, model

    def test_split_fd_interpolation_keeps_its_role_and_does_not_accept_native_fit(self):
        result = self.convert(*self.split_material_fixture())
        self.assertEqual(result['status'], 'diagnostic_only')
        report = result['material_fit_readback']
        self.assertEqual(report['status'], 'profile_declared_fd_interpolation_matches_samples')
        self.assertEqual(report['native_acceptance'], 'pending')
        self.assertIn('material_fit_readback', result['unresolved_gates'])
        self.assertIn('material_solver_response_linkage', result['unresolved_gates'])
        point = report['materials'][0]['components']['epsilon']['points'][0]
        self.assertEqual(point['actual_imag'], -0.1)
        self.assertEqual(report['materials'][0]['source_role'], 'fd_interpolated')
        self.assertFalse(result['physical_accepted'])

    def test_split_fit_selects_only_exact_planned_points_from_matching_dense_grid(self):
        raw, profile, case, model = self.split_material_fixture('nth_order_fit')
        for entry in profile['materials']:
            for component in ('epsilon', 'mu'):
                for field in ('real', 'loss'):
                    selector = entry[component][field]
                    curve = next(c for c in raw['curves'] if c['treepath'] == selector['treepath'])
                    curve['x'] = [1, 1.25, 1.75, 2]
                    curve['y'] = [curve['y'][0], 100, 100, curve['y'][-1]]
                    curve['point_count'] = 4
        result = self.convert(raw, profile, case, model)
        self.assertEqual(result['status'], 'diagnostic_only')
        report = result['material_fit_readback']['materials'][0]['components']['epsilon']
        self.assertEqual(report['actual_frequencies_Hz'], [1e9, 1.25e9, 1.75e9, 2e9])
        self.assertEqual([p['frequency_Hz'] for p in report['points']], [1e9, 2e9])
        self.assertIn('material_solver_response_linkage', result['unresolved_gates'])

    def test_missing_exact_material_sample_stays_unresolved_without_interpolation(self):
        raw, profile, case, model = self.split_material_fixture('nth_order_fit')
        for entry in profile['materials']:
            for component in ('epsilon', 'mu'):
                for field in ('real', 'loss'):
                    selector = entry[component][field]
                    curve = next(c for c in raw['curves'] if c['treepath'] == selector['treepath'])
                    curve['x'] = [1, 1.49, 2]
                    curve['y'] = [curve['y'][0], 100, curve['y'][-1]]
                    curve['point_count'] = 3
            sample = case['material_samples'][entry['material_id']]
            sample['frequencies_Hz'] = [1e9, 1.5e9, 2e9]
            for key in ('epsilon_real', 'epsilon_imag', 'mu_real', 'mu_imag'):
                sample[key] = [sample[key][0], 100, sample[key][-1]]
        case['scenario']['frequencies_Hz'] = [1e9, 1.5e9, 2e9]
        # Keep independent scattering/power quantities complete on the new plan.
        for curve in raw['curves']:
            if not curve['treepath'].startswith('1D Results\\Materials\\'):
                curve['x'] = [1, 1.5, 2]
                curve['y'].insert(1, copy.deepcopy(curve['y'][0]))
                curve['point_count'] = 3
        result = self.convert(raw, profile, case, model)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('material_sample_coverage', result['unresolved_gates'])
        report = result['material_fit_readback']['materials'][0]
        self.assertFalse(report['comparison_complete'])
        self.assertIsNone(report['matched'])
        self.assertEqual(report['components']['epsilon']['missing_frequencies_Hz'], [1.5e9])
        self.assertTrue((self.root / 'canonical' / 'material-fit-readback.json').exists())

    def test_data_list_or_fd_leaf_cannot_be_declared_fit_even_with_fit_title(self):
        original = self.fixture()
        for index, suffix in enumerate(('Data list', 'FD - Interpolated')):
            raw, profile, case, model = copy.deepcopy(original)
            selector = profile['materials'][0]['epsilon']
            curve = next(c for c in raw['curves'] if c['treepath'] == selector['treepath'])
            path = r"1D Results\Materials\mat_a\Dispersive\Eps' (" + suffix + ')'
            curve['treepath'] = curve['reported_treepath'] = selector['treepath'] = path
            curve['title'] = selector['title'] = 'Electric Dispersion: Nth Order Model, N=1 (Fit)'
            result = self.convert(raw, profile, case, model, str(index))
            self.assertEqual(result['status'], 'failed')
            self.assertIn('material_source_role_identity', result['unresolved_gates'])

    def test_split_component_source_identity_and_grid_are_exact(self):
        original = self.split_material_fixture()
        for index, change in enumerate(('role', 'material', 'component', 'grid', 'complex', 'negative_loss', 'policy', 'convention')):
            raw, profile, case, model = copy.deepcopy(original)
            entry = profile['materials'][0]
            selector = entry['epsilon']['loss']
            curve = next(c for c in raw['curves'] if c['treepath'] == selector['treepath'])
            if change in ('role', 'material', 'component'):
                path = curve['treepath'].replace('FD - Interpolated', 'Fit') if change == 'role' else curve['treepath'].replace('mat_a', 'mat_b') if change == 'material' else curve['treepath'].replace("Eps''", "Mu''")
                curve['treepath'] = curve['reported_treepath'] = selector['treepath'] = path
            elif change == 'grid':
                curve['x'] = [1, 2.0001]
            elif change == 'complex':
                curve['y'][0] = {'real': 0.1, 'imag': 0.2}
            elif change == 'negative_loss':
                curve['y'][0] = -0.1
            elif change == 'policy':
                entry['epsilon']['sample_policy'] = 'linear_interpolate'
            else:
                entry['source_time_convention'] = 'exp(-jωt)'
            result = self.convert(raw, profile, case, model, str(index))
            self.assertEqual(result['status'], 'failed', change)
            self.assertIsNone(result['spectra_path'])

    def test_split_material_conductivity_cannot_be_counted_twice(self):
        original = self.split_material_fixture()
        for index, location in enumerate(('profile', 'prepared')):
            raw, profile, case, model = copy.deepcopy(original)
            if location == 'profile':
                profile['materials'][0]['native_conductivity_S_m'] = 1
            else:
                case['material_samples']['a']['conductivity_S_m'] = 1
            result = self.convert(raw, profile, case, model, str(index))
            self.assertEqual(result['status'], 'failed')
            self.assertIn('conductivity', str(result['errors']).lower())

    def test_unrecognized_legacy_complex_fit_role_remains_declared_only(self):
        result = self.convert(*self.fixture())
        self.assertEqual(result['status'], 'diagnostic_only')
        self.assertIn('material_source_role_identity', result['unresolved_gates'])
        self.assertIn('material_solver_response_linkage', result['unresolved_gates'])

    def test_split_fit_loss_error_is_preserved_in_a_failed_numeric_report(self):
        raw, profile, case, model = self.split_material_fixture('nth_order_fit')
        sample = case['material_samples']['a']
        sample.update(epsilon_real=[3, 3], epsilon_imag=[-0.2, -0.2])
        component = profile['materials'][0]['epsilon']
        for field, values in [('real', [3, 3]), ('loss', [0.124952, 0.247053])]:
            curve = next(c for c in raw['curves'] if c['treepath'] == component[field]['treepath'])
            curve['y'] = values
        result = self.convert(raw, profile, case, model)
        self.assertEqual(result['status'], 'failed')
        report = result['material_fit_readback']
        self.assertEqual(report['status'], 'fit_sample_mismatch')
        self.assertTrue(report['comparison_complete'])
        self.assertFalse(report['materials'][0]['matched'])
        points = report['materials'][0]['components']['epsilon']['points']
        self.assertAlmostEqual(points[0]['actual_imag'], -0.124952)
        self.assertAlmostEqual(points[0]['absolute_error'], 0.075048)
        self.assertFalse(points[1]['within_declared_tolerance'])
        self.assertTrue((self.root / 'canonical' / 'material-fit-readback.json').exists())

    def native_axis_fixture(self):
        raw, profile, case, model = self.split_material_fixture()
        profile['excitation']['s_identity'] = {'field': 'treepath',
            'template': r'1D Results\S-Parameters\S{receive_port}({receive_mode}),{incident_port}({incident_mode})'}
        for mode in profile['modes']:
            selector = mode['reflection']
            curve = next(c for c in raw['curves'] if c['treepath'] == selector['treepath'])
            path = r'1D Results\S-Parameters\SZmax(' + str(mode['native_mode_index']) + '),Zmax(7)'
            curve.update(treepath=path, reported_treepath=path, title='S-Parameters', ylabel='')
            selector.update(treepath=path, title='S-Parameters', ylabel='', y_unit='1')
        for selector in [profile['power']['stimulated'], profile['power']['reflected'],
                         *[entry['selector'] for entry in profile['power']['material_absorbed']]]:
            curve = next(c for c in raw['curves'] if c['treepath'] == selector['treepath'])
            selector['ylabel'] = curve['ylabel'] = 'W'
        return raw, profile, case, model

    def test_actual_native_s_blank_and_literal_power_unit_keep_native_axis_labels(self):
        result = self.convert(*self.native_axis_fixture())
        self.assertEqual(result['status'], 'diagnostic_only')
        sources = result['sources']
        self.assertTrue(all(s['ylabel'] == '' and s['y_unit'] == '1' for s in sources if s['role'] == 'reflection'))
        self.assertTrue(all(s['ylabel'] == 'W' for s in sources if s['role'] == 'power'))
        self.assertFalse(result['physical_accepted'])

    def test_literal_power_unit_is_supported_with_existing_nonblank_s_profile(self):
        raw, profile, case, model = self.fixture()
        for selector in [profile['power']['stimulated'], profile['power']['reflected'],
                         *[entry['selector'] for entry in profile['power']['material_absorbed']]]:
            curve = next(c for c in raw['curves'] if c['treepath'] == selector['treepath'])
            selector['ylabel'] = curve['ylabel'] = 'W'
        result = self.convert(raw, profile, case, model)
        self.assertEqual(result['status'], 'diagnostic_only')

    def test_blank_axis_requires_actual_native_s_role_and_exact_incident_column(self):
        original = self.native_axis_fixture()
        for index, change in enumerate(('wrong_role', 'adaptive', 'incident', 'receiving')):
            raw, profile, case, model = copy.deepcopy(original)
            selector = profile['modes'][0]['reflection']
            curve = next(c for c in raw['curves'] if c['treepath'] == selector['treepath'])
            if change in ('wrong_role', 'adaptive'):
                # Joining avoids turning a path separator into a string terminator.
                prefix = '\\'.join(('1D Results', 'Power' if change == 'wrong_role' else 'Adaptive Meshing', 'f=2', 'S-Parameters')) + '\\'
                profile['excitation']['s_identity']['template'] = prefix + 'S{receive_port}({receive_mode}),{incident_port}({incident_mode})'
                for mode in profile['modes']:
                    selected = mode['reflection']
                    actual = next(c for c in raw['curves'] if c['treepath'] == selected['treepath'])
                    path = prefix + 'SZmax(' + str(mode['native_mode_index']) + '),Zmax(7)'
                    actual['treepath'] = actual['reported_treepath'] = selected['treepath'] = path
            else:
                path = curve['treepath'].replace(',Zmax(7)', ',Zmax(9)') if change == 'incident' else curve['treepath'].replace('SZmax(7)', 'SZmax(9)')
                curve['treepath'] = curve['reported_treepath'] = selector['treepath'] = path
            result = self.convert(raw, profile, case, model, str(index))
            self.assertEqual(result['status'], 'failed', change)
            self.assertIsNone(result['spectra_path'])

    def test_bare_power_units_must_match_literal_unit_without_ambiguous_whitespace(self):
        original = self.native_axis_fixture()
        for index, label in enumerate((' W', 'W ', 'mW', 'Power W', '', 'dimensionless / 1')):
            raw, profile, case, model = copy.deepcopy(original)
            selector = profile['power']['stimulated']
            curve = next(c for c in raw['curves'] if c['treepath'] == selector['treepath'])
            curve['ylabel'] = selector['ylabel'] = label
            result = self.convert(raw, profile, case, model, str(index))
            self.assertEqual(result['status'], 'failed', repr(label))
            self.assertIsNone(result['power_path'])


if __name__ == '__main__':
    unittest.main()
