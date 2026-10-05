import builtins
import copy
import importlib
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


def case_fixture():
    from test_contracts import config_fixture
    from cst_absorber.contracts import build_plan
    plan = build_plan(config_fixture(), Path('.'))
    case = plan['cases'][0]
    case['runtime'] = plan['runtime']
    return case


def model_text():
    return '\n'.join([
        'schema\tnative-model-v1', 'unit\tlength\tmm', 'unit\tfrequency\tGHz',
        'factor\tgeometry_to_SI\t0.001', 'factor\tgeometry_SI_to_unit\t1000',
        'factor\tfrequency_to_SI\t1e9', 'factor\tfrequency_SI_to_unit\t1e-9',
        'domain\t0\t10\t0\t10\t0\t12', 'structure\t0\t10\t0\t10\t0\t1',
        'lattice\t10\t10\t90',
        'boundary\tXmin\tunit cell', 'boundary\tXmax\tunit cell',
        'boundary\tYmin\tunit cell', 'boundary\tYmax\tunit cell',
        'boundary\tZmin\telectric', 'boundary\tZmax\topen',
        'shape_count\t1', 'shape_name\t0\tabsorber:region_layer',
        'shape\tabsorber:region_layer\tmat_a\t100\t0\t10\t0\t10\t0\t1',
        'mode_port\tZmax', 'mode_count\t2', 'mode\t1\tTM(0,0)', 'mode\t2\tTE(0,0)',
    ]) + '\n'


def mesh_text(edge='.4', threads='2'):
    return '\n'.join(['schema\tnative-mesh-v1', 'unit\tlength\tmm',
                      'factor\tgeometry_to_SI\t0.001', 'max_edge\t' + edge,
                      'mesh_type\tTetrahedral', 'mesh_cells\t3750', 'min_edge\t.1',
                      'mesher_mode\tuser-defined', 'mesher_threads\t' + threads,
                      'source\tmesh_type\tMesh.GetMeshType',
                      'source\tmesh_cells\tMesh.GetNumberOfMeshCells',
                      'source\tmin_edge\tMesh.GetMinimumEdgeLength',
                      'source\tmax_edge\tMesh.GetMaximumEdgeLength',
                      'source\tmesher_mode\tMesh.GetParallelMesherMode("Tet")',
                      'source\tmesher_threads\tMesh.GetMaxParallelMesherThreads("Tet")']) + '\n'


def cube_readback_fixture(edge_m, *, base_z_m=0, length_unit='mm'):
    """Canonical SI cube plus independently serialized native-unit report."""
    from test_contracts import config_fixture
    from cst_absorber.contracts import build_plan
    raw = config_fixture()
    physical = raw['case']
    top = base_z_m + edge_m
    physical['geometry']['cell'] = {'Lx_m': edge_m, 'Ly_m': edge_m, 'height_m': top}
    physical['geometry']['regions'][0]['bounds_m'] = [[0, 0, base_z_m], [edge_m, edge_m, top]]
    physical['scenario'].update(air_height_m=edge_m, reference_plane_m=edge_m / 2)
    physical['mesh']['max_edge_m'] = edge_m / 4
    case = build_plan(raw, Path('.'))['cases'][0]
    factor = {'nm': 1e-9, 'mm': 1e-3, 'm': 1.0}[length_unit]
    def native(value):
        return format(value / factor, '.17g')
    def box(bbox):
        return [native(bbox[row][axis]) for axis in range(3) for row in range(2)]
    rows = [row.split('\t') for row in model_text().splitlines()]
    for row in rows:
        if row[:2] == ['unit', 'length']:
            row[2] = length_unit
        elif row[:2] == ['factor', 'geometry_to_SI']:
            row[2] = format(factor, '.17g')
        elif row[:2] == ['factor', 'geometry_SI_to_unit']:
            row[2] = format(1 / factor, '.17g')
        elif row[0] == 'domain':
            row[1:] = box([[0, 0, 0], [edge_m, edge_m, top + edge_m]])
        elif row[0] == 'structure':
            row[1:] = box(case['geometry_audit']['bbox_m'])
        elif row[0] == 'lattice':
            row[1:3] = [native(edge_m), native(edge_m)]
        elif row[0] == 'shape':
            row[3] = format(case['geometry_audit']['volumes_m3']['layer'] / factor ** 3, '.17g')
            row[4:] = box(case['geometry_audit']['region_bboxes_m']['layer'])
    return case, '\n'.join('\t'.join(row) for row in rows) + '\n'


def mutate_report_number(text, tag, column, transform):
    rows = [row.split('\t') for row in text.splitlines()]
    for row in rows:
        if row[0] == tag:
            row[column] = format(transform(float(row[column])), '.17g')
    return '\n'.join('\t'.join(row) for row in rows) + '\n'


class NativeModelTests(unittest.TestCase):
    def setUp(self):
        try:
            self.module = importlib.import_module('cst_absorber.native_model')
        except ModuleNotFoundError:
            self.module = None
        self.assertIsNotNone(self.module, 'documented native macros and report validation must exist')
        self.case = case_fixture()

    def write_model(self, directory, text=None):
        root = Path(directory)
        (root / 'native_model.tsv').write_text(model_text() if text is None else text, encoding='utf-8')
        return self.module.read_model_report(self.case, root)

    def test_binding_changes_only_placeholder_and_blocks_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            text = 'Open "@@ARTIFACT_ROOT@@/native_model.tsv" For Output As #fh\nName "sample"\n'
            bound = self.module.bind_artifact_text(text, Path(directory))
            self.assertNotIn('@@ARTIFACT_ROOT@@', bound)
            self.assertIn('Name "sample"', bound)
            self.assertIn(Path(directory).resolve().as_posix(), bound)
            with self.assertRaisesRegex(ValueError, 'escape|traversal'):
                self.module.bind_artifact_text('Open "@@ARTIFACT_ROOT@@/../foreign.tsv"', Path(directory))

    def test_original_macro_queries_real_domain_units_shapes_and_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            text = self.module.model_readback_vba(self.case, Path(directory))
            for name in ['Units.GetUnit("Length")', 'Units.GetGeometryUnitToSI',
                         'Units.GetFrequencyUnitToSI', 'Boundary.GetCalculationBox',
                         'Boundary.GetStructureBox', 'Boundary.GetZmin',
                         'Boundary.GetUnitCellDs1', 'Solid.GetNumberOfShapes',
                         'Solid.GetNameOfShapeFromIndex',
                         'Solid.GetMaterialNameForShape', 'Solid.GetVolume',
                         'Solid.GetLooseBoundingBoxOfShape', 'FloquetPort.GetModeNameByNumber',
                         'FloquetPort.IsPortAtZmax']:
                self.assertIn(name, text)
            self.assertNotIn('GetDistanceToReferencePlane', text)
            self.assertIn('native_model.tsv', text)
            self.assertNotIn('@@ARTIFACT_ROOT@@', text)

    def test_macro_stops_unexpected_shape_or_mode_counts_before_unbounded_iteration(self):
        with tempfile.TemporaryDirectory() as directory:
            text = self.module.model_readback_vba(self.case, Path(directory))
            shape_guard = text.find('If count <> 1 Then Err.Raise')
            mode_guard = text.find('If count <> 2 Then Err.Raise')
            self.assertGreaterEqual(shape_guard, 0)
            self.assertGreaterEqual(mode_guard, 0)
            self.assertLess(shape_guard, text.find('For i = 0 To count'))
            self.assertLess(mode_guard, text.find('For i = 1 To count'))

    def test_impossible_shape_query_index_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            text = model_text().replace('shape_name\t0\t', 'shape_name\t999\t')
            with self.assertRaisesRegex(ValueError, 'shape.*index'):
                self.write_model(directory, text)

    def test_readback_checks_consistency_but_retains_pending_native_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            report = self.write_model(directory)
            self.assertEqual(report['status'], 'model_report_validated')
            self.assertEqual(report['native_validation'], 'not_established')
            self.assertFalse(report['numerically_qualified'])
            self.assertAlmostEqual(report['domain_m'][1][2], .012)
            self.assertAlmostEqual(report['shapes'][0]['volume_m3'], 1e-7)
            self.assertEqual(report['modes'][0]['polarization'], 'TM')
            self.assertEqual(report['modes'][0]['index'], 1)
            self.assertEqual(report['modes'][0]['port'], 'Zmax')
            self.assertEqual(report['modes_port'], 'Zmax')
            self.assertIn('material_fit_readback', report['unresolved_gates'])
            self.assertIn('reference_plane_readback', report['unresolved_gates'])
            self.assertIsNotNone(json.dumps(report, allow_nan=False))

    def test_missing_duplicate_unknown_and_truncated_records_fail(self):
        variants = [model_text().replace('lattice\t10\t10\t90\n', ''),
                    model_text() + 'shape_count\t1\n',
                    model_text() + 'fabricated_getter\tpassed\n',
                    model_text().replace('domain\t0\t10\t0\t10\t0\t12', 'domain\t0\t10')]
        for variant in variants:
            with self.subTest(text=variant), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    self.write_model(directory, variant)

    def test_actual_units_and_factors_must_match(self):
        for old, new in [('unit\tlength\tmm', 'unit\tlength\tm'),
                         ('factor\tgeometry_to_SI\t0.001', 'factor\tgeometry_to_SI\t0.002'),
                         ('factor\tfrequency_SI_to_unit\t1e-9', 'factor\tfrequency_SI_to_unit\t1'),
                         ('factor\tgeometry_to_SI\t0.001', 'factor\tgeometry_to_SI\tnan')]:
            with self.subTest(new=new), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ValueError, 'unit|factor|finite'):
                    self.write_model(directory, model_text().replace(old, new))

    def test_domain_lattice_boundary_and_structure_mismatch_fail(self):
        for old, new in [('domain\t0\t10\t0\t10\t0\t12', 'domain\t1\t11\t0\t10\t0\t12'),
                         ('lattice\t10\t10\t90', 'lattice\t4\t10\t90'),
                         ('boundary\tZmin\telectric', 'boundary\tZmin\topen'),
                         ('structure\t0\t10\t0\t10\t0\t1', 'structure\t0\t10\t0\t10\t0\t2')]:
            with self.subTest(new=new), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    self.write_model(directory, model_text().replace(old, new))

    def test_shape_material_count_volume_and_bounds_mismatch_fail(self):
        for old, new in [('shape_count\t1', 'shape_count\t2'),
                         ('\tmat_a\t100', '\tmat_foreign\t100'),
                         ('\tmat_a\t100', '\tmat_a\t90'),
                         ('\tmat_a\t100\t0\t10', '\tmat_a\t100\t1\t10')]:
            with self.subTest(new=new), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    self.write_model(directory, model_text().replace(old, new))

    def test_small_legitimate_volume_and_bounds_roundoff_passes_without_minimum_scale(self):
        for edge in [1e-7, 1e-9, 1e-12, 1e-20]:
            self.case, text = cube_readback_fixture(edge)
            text = mutate_report_number(text, 'shape', 3, lambda value: value * (1 + 4e-6))
            text = mutate_report_number(text, 'shape', 5, lambda value: value * (1 + 4e-7))
            text = mutate_report_number(text, 'lattice', 1, lambda value: value * (1 + 4e-7))
            with self.subTest(edge_m=edge), tempfile.TemporaryDirectory() as directory:
                report = self.write_model(directory, text)
                self.assertAlmostEqual(report['shapes'][0]['volume_m3'] /
                    self.case['geometry_audit']['volumes_m3']['layer'], 1 + 4e-6, places=10)
                self.assertFalse(report['numerically_qualified'])

    def test_100nm_volume_gross_and_percent_mismatches_fail(self):
        self.case, text = cube_readback_fixture(1e-7)
        self.assertAlmostEqual(self.case['geometry_audit']['volumes_m3']['layer'] / 1e-21, 1)
        for multiplier in [100, 1.01, .5]:
            wrong = mutate_report_number(text, 'shape', 3, lambda value: value * multiplier)
            with self.subTest(multiplier=multiplier), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ValueError, 'shape volume'):
                    self.write_model(directory, wrong)

    def test_small_domain_structure_shape_and_lattice_errors_fail(self):
        self.case, text = cube_readback_fixture(1e-12)
        # All SI errors below the former 1e-10 m floor, but gross for this cube.
        variants = [('domain', 1, lambda value: 5e-10),
                    ('structure', 6, lambda value: value * 2),
                    ('shape', 7, lambda value: value * 2),
                    ('lattice', 1, lambda value: value * 100)]
        for tag, column, change in variants:
            wrong = mutate_report_number(text, tag, column, change)
            with self.subTest(tag=tag), tempfile.TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ValueError, 'domain|structure|shape|lattice'):
                    self.write_model(directory, wrong)

    def test_translated_thin_shape_cannot_use_coordinate_magnitude_as_extent_tolerance(self):
        self.case, text = cube_readback_fixture(1e-7, base_z_m=.001)
        # 0.2% thickness error is small compared with the absolute z coordinate.
        wrong = mutate_report_number(text, 'shape', 9, lambda value: value + 2e-7)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'shape bounds'):
                self.write_model(directory, wrong)

    def test_opposed_endpoints_cannot_double_the_axis_extent_tolerance(self):
        self.case, text = cube_readback_fixture(1e-7)
        wrong = mutate_report_number(text, 'shape', 4, lambda value: value + .75e-10)
        wrong = mutate_report_number(wrong, 'shape', 5, lambda value: value - .75e-10)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'shape bounds'):
                self.write_model(directory, wrong)

    def test_existing_large_cell_origin_tolerance_is_not_relaxed(self):
        wrong = mutate_report_number(model_text(), 'domain', 1, lambda value: .2e-6)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'calculation domain'):
                self.write_model(directory, wrong)

    def test_small_cube_native_nm_mm_m_units_are_equivalent(self):
        for unit in ['nm', 'mm', 'm']:
            self.case, text = cube_readback_fixture(1e-7, length_unit=unit)
            with self.subTest(unit=unit), tempfile.TemporaryDirectory() as directory:
                report = self.write_model(directory, text)
                self.assertAlmostEqual(report['shapes'][0]['volume_m3'] / 1e-21, 1)
                self.assertAlmostEqual(report['domain_m'][1][0] / 1e-7, 1)

    def test_mode_indices_names_count_port_and_duplicates_are_checked(self):
        for old, new in [('mode_count\t2', 'mode_count\t1'),
                         ('mode\t2\tTE(0,0)', 'mode\t1\tTE(0,0)'),
                         ('mode\t2\tTE(0,0)', 'mode\t2\tTE(1,0)'),
                         ('mode_port\tZmax', 'mode_port\tZmin'),
                         ('mode\t2\tTE(0,0)', 'mode\t2\tLCP')]:
            with self.subTest(new=new), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    self.write_model(directory, model_text().replace(old, new))

    def test_sparse_multi_region_bbox_does_not_shrink_domain(self):
        from cst_absorber.contracts import build_plan
        case = self.case
        physical = {key: copy.deepcopy(case[key]) for key in ('id', 'geometry', 'materials', 'scenario', 'mesh', 'analysis')}
        physical['geometry']['regions'][0]['bounds_m'][1] = [.004, .006, .001]
        physical['geometry']['regions'].append({'id': 'top', 'material': 'a', 'kind': 'brick',
            'bounds_m': [[0, 0, .001], [.004, .006, .002]]})
        self.case = build_plan({'schema_version': '1.0', 'case': physical, 'runtime': case['runtime']}, Path('.'))['cases'][0]
        text = model_text().replace('structure\t0\t10\t0\t10\t0\t1', 'structure\t0\t4\t0\t6\t0\t2')
        text = text.replace('shape_count\t1', 'shape_count\t2').replace(
            'shape_name\t0\tabsorber:region_layer', 'shape_name\t0\tabsorber:region_layer\nshape_name\t1\tabsorber:region_top').replace(
            'shape\tabsorber:region_layer\tmat_a\t100\t0\t10\t0\t10\t0\t1',
            'shape\tabsorber:region_layer\tmat_a\t24\t0\t4\t0\t6\t0\t1\nshape\tabsorber:region_top\tmat_a\t24\t0\t4\t0\t6\t1\t2')
        with tempfile.TemporaryDirectory() as directory:
            report = self.write_model(directory, text)
            self.assertAlmostEqual(report['domain_m'][1][0], .01)
            self.assertEqual(len(report['shapes']), 2)

    def test_cpu_and_mesh_commands_use_actual_domain_and_named_solids(self):
        with tempfile.TemporaryDirectory() as directory:
            report = self.write_model(directory)
            text = self.module.resource_mesh_vba(self.case, self.case['runtime'], report)
            self.assertIn('With FDSolver', text)
            self.assertIn('.MaxCPUs "2"', text)
            self.assertIn('.MaximumNumberOfCPUDevices "1"', text)
            self.assertIn('Mesh.SetParallelMesherMode "Tet", "user-defined"', text)
            self.assertIn('Mesh.SetMaxParallelMesherThreads "Tet", "2"', text)
            self.assertIn('Mesh.MinimumStepNumberTet "38"', text)
            self.assertIn('Solid.SetMeshStepwidthTet "absorber:region_layer", "0.5"', text)
            self.assertIn('Mesh.DelaunayPropagationFactor "1"', text)
            for cpus in [0, True, 1.5]:
                with self.assertRaises(ValueError):
                    self.module.resource_mesh_vba(self.case, dict(self.case['runtime'], max_cpus=cpus), report)

    def test_mesh_macro_queries_actual_selected_mesh_and_mesher_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            text = self.module.mesh_readback_vba(Path(directory))
            self.assertIn('Mesh.GetMaximumEdgeLength', text)
            self.assertIn('Mesh.GetParallelMesherMode("Tet")', text)
            self.assertIn('Mesh.GetMaxParallelMesherThreads("Tet")', text)
            self.assertIn('Units.GetGeometryUnitToSI', text)
            self.assertIn('native_mesh.tsv', text)

    def test_mesh_report_retains_raw_value_and_unit_assumption(self):
        with tempfile.TemporaryDirectory() as directory:
            report = self.write_model(directory)
            (Path(directory) / 'native_mesh.tsv').write_text(mesh_text(), encoding='utf-8')
            mesh = self.module.read_mesh_report(self.case, Path(directory), report)
            self.assertAlmostEqual(mesh['max_edge_raw'], .4)
            self.assertAlmostEqual(mesh['max_edge_m_under_assumption'], .0004)
            self.assertTrue(mesh['edge_within_limit_under_assumption'])
            self.assertFalse(mesh['mesh_unit_confirmed'])
            self.assertFalse(mesh['strict_mesher_size_guarantee'])
            self.assertIn('mesh_edge_unit', mesh['unresolved_gates'])

    def test_mesh_reports_actual_type_count_and_getter_provenance_without_enforcement(self):
        with tempfile.TemporaryDirectory() as directory:
            report = self.write_model(directory)
            (Path(directory) / 'native_mesh.tsv').write_text(mesh_text(), encoding='utf-8')
            mesh = self.module.read_mesh_report(self.case, Path(directory), report)
            self.assertEqual(mesh['mesh_type'], 'Tetrahedral')
            self.assertEqual(mesh['mesh_cells'], 3750)
            self.assertEqual(mesh['min_edge_raw'], .1)
            self.assertEqual(mesh['query_provenance']['mesh_cells']['getter'], 'Mesh.GetNumberOfMeshCells')
            self.assertEqual(mesh['mesher_threads_evidence'], 'configured_process_count')
            self.assertFalse(mesh['mesher_thread_enforcement_verified'])
            self.assertIn('mesh_edge_freshness', mesh['unresolved_gates'])
            self.assertIn('mesher_thread_enforcement', mesh['unresolved_gates'])
            self.assertIn('solver_cpu_enforcement', mesh['unresolved_gates'])

    def test_mesh_macro_includes_documented_statistics_and_no_guessed_cpu_or_plane_getter(self):
        with tempfile.TemporaryDirectory() as directory:
            text = self.module.mesh_readback_vba(Path(directory))
            for getter in ('Mesh.GetMeshType', 'Mesh.GetNumberOfMeshCells', 'Mesh.GetMinimumEdgeLength'):
                self.assertIn(getter, text)
            self.assertNotIn('GetMaxCPUs', text)
            self.assertNotIn('GetDistanceToReferencePlane', text)

    def test_mesh_missing_duplicate_bad_provenance_type_count_and_minimum_fail(self):
        variants = [mesh_text().replace('mesh_cells\t3750\n', ''),
                    mesh_text() + 'mesh_cells\t3750\n',
                    mesh_text().replace('mesh_cells\t3750', 'mesh_cells\t0'),
                    mesh_text().replace('mesh_cells\t3750', 'mesh_cells\t3.5'),
                    mesh_text().replace('mesh_type\tTetrahedral', 'mesh_type\tPBA'),
                    mesh_text().replace('min_edge\t.1', 'min_edge\t.5'),
                    mesh_text().replace('min_edge\t.1', 'min_edge\tnan'),
                    mesh_text().replace('source\tmesh_type\tMesh.GetMeshType\n', ''),
                    mesh_text() + 'source\tmesh_type\tMesh.GetMeshType\n',
                    mesh_text().replace('source\tmesh_cells\tMesh.GetNumberOfMeshCells',
                                        'source\tmesh_cells\trequested_configuration')]
        for text in variants:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as directory:
                report = self.write_model(directory)
                (Path(directory) / 'native_mesh.tsv').write_text(text, encoding='utf-8')
                with self.assertRaises(ValueError):
                    self.module.read_mesh_report(self.case, Path(directory), report)

    def test_missing_public_plane_and_cpu_getters_are_explicit_limitation_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            report = self.write_model(directory)
            plane = report['readback_limitations']['reference_plane_readback']
            self.assertEqual(plane['status'], 'unsupported_public_getter')
            self.assertEqual(plane['port'], 'Zmax')
            self.assertEqual(plane['requested_zref_m'], .007)
            self.assertFalse(plane['configured_value_verified'])
            self.assertFalse(plane['phase_benchmark_verified'])
            self.assertIn('solver_cpu_enforcement', report['unresolved_gates'])

    def test_mesh_missing_nonfinite_nonpositive_oversize_and_units_fail(self):
        for text in [mesh_text('nan'), mesh_text('0'), mesh_text('-.1'), mesh_text('.6'),
                     mesh_text().replace('unit\tlength\tmm', 'unit\tlength\tm'),
                     mesh_text().replace('mesher_threads\t2', 'mesher_threads\t0')]:
            with self.subTest(text=text), tempfile.TemporaryDirectory() as directory:
                report = self.write_model(directory)
                (Path(directory) / 'native_mesh.tsv').write_text(text, encoding='utf-8')
                with self.assertRaises(ValueError):
                    self.module.read_mesh_report(self.case, Path(directory), report)
        with tempfile.TemporaryDirectory() as directory:
            report = self.write_model(directory)
            with self.assertRaises(ValueError):
                self.module.read_mesh_report(self.case, Path(directory), report)

    def test_all_macro_and_parser_paths_avoid_sdk_imports(self):
        original = builtins.__import__
        def forbid(name, *args, **kwargs):
            if name == 'cst' or name.startswith('cst.'):
                raise AssertionError('native_model must have no SDK import side effects')
            return original(name, *args, **kwargs)
        with tempfile.TemporaryDirectory() as directory, patch('builtins.__import__', side_effect=forbid):
            report = self.write_model(directory)
            self.module.model_readback_vba(self.case, Path(directory))
            self.module.resource_mesh_vba(self.case, self.case['runtime'], report)
            self.module.mesh_readback_vba(Path(directory))
            (Path(directory) / 'native_mesh.tsv').write_text(mesh_text(), encoding='utf-8')
            self.module.read_mesh_report(self.case, Path(directory), report)


if __name__ == '__main__':
    unittest.main()
