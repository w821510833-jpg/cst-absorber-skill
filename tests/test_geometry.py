import copy
import importlib
import tempfile
import unittest
from pathlib import Path
import trimesh
from test_contracts import case_fixture, config_fixture


class GeometryTests(unittest.TestCase):
    def setUp(self):
        try:
            self.module = importlib.import_module('cst_absorber.geometry')
        except ModuleNotFoundError:
            self.module = None
        self.assertIsNotNone(self.module, 'geometry auditing must be implemented')
        self.geometry = case_fixture()['geometry']

    def test_brick_volume_and_adjacent_materials(self):
        self.geometry['regions'].append({'id': 'upper', 'material': 'b', 'kind': 'brick',
                                         'bounds_m': [[0, 0, .001], [.01, .01, .002]]})
        result = self.module.audit_geometry(self.geometry, Path('.'))
        self.assertAlmostEqual(result['volumes_m3']['layer'], 1e-7)
        self.assertAlmostEqual(result['volumes_m3']['upper'], 1e-7)
        self.assertEqual(result['overlap_status'], 'verified_no_overlap')

    def test_bbox_outside_and_brick_overlap_fail(self):
        malformed = case_fixture()['geometry']
        malformed['cell']['Lx_m'] = 10**1000
        with self.assertRaises(ValueError):
            self.module.audit_geometry(malformed, Path('.'))
        self.geometry['regions'][0]['bounds_m'][1][0] = .02
        with self.assertRaises(ValueError):
            self.module.audit_geometry(self.geometry, Path('.'))
        self.geometry = case_fixture()['geometry']
        self.geometry['regions'].append(dict(self.geometry['regions'][0], id='other'))
        with self.assertRaises(ValueError):
            self.module.audit_geometry(self.geometry, Path('.'))

    def test_closed_stl_unit_transform_volume_hash_and_open_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'box.stl'
            box = trimesh.creation.box(extents=[10, 10, 1])
            box.export(path)
            geometry = {'cell': self.geometry['cell'], 'regions': [{
                'id': 'stl', 'material': 'a', 'kind': 'stl', 'path': 'box.stl',
                'source_unit': 'mm', 'transform': {'scale': 1, 'translation_m': [.005, .005, .0005]}}]}
            result = self.module.audit_geometry(geometry, Path(directory))
            self.assertAlmostEqual(result['volumes_m3']['stl'], 1e-7)
            self.assertEqual(len(result['asset_hashes']['stl']), 64)
            first_hash = result['asset_hashes']['stl']
            trimesh.creation.box(extents=[8, 10, 1]).export(path)
            self.assertNotEqual(first_hash, self.module.audit_geometry(geometry, Path(directory))['asset_hashes']['stl'])
            box.update_faces(list(range(len(box.faces) - 1)))
            box.export(path)
            with self.assertRaises(ValueError):
                self.module.audit_geometry(geometry, Path(directory))

    def test_unsupported_transform_and_unknown_material_mapping_fail(self):
        self.geometry['regions'][0]['rotation'] = [0, 0, 90]
        with self.assertRaises(ValueError):
            self.module.audit_geometry(self.geometry, Path('.'))
        contracts = importlib.import_module('cst_absorber.contracts')
        raw = config_fixture()
        raw['case']['geometry']['regions'][0]['material'] = 'missing'
        with self.assertRaises(ValueError):
            contracts.build_plan(raw, Path('.'))

    def test_intersecting_stl_bbox_is_reported_unverified(self):
        with tempfile.TemporaryDirectory() as directory:
            trimesh.creation.box(extents=[.002, .002, .0005]).export(Path(directory) / 'small.stl')
            self.geometry['regions'].append({'id': 'stl', 'material': 'a', 'kind': 'stl',
                'path': 'small.stl', 'source_unit': 'm',
                'transform': {'scale': 1, 'translation_m': [.005, .005, .0005]}})
            result = self.module.audit_geometry(self.geometry, Path(directory))
            self.assertEqual(result['overlap_status'], 'unverified')
            self.assertEqual(result['unverified_intersections'], [['layer', 'stl']])

    def test_disconnected_nested_shells_do_not_claim_valid_volume(self):
        with tempfile.TemporaryDirectory() as directory:
            outer = trimesh.creation.box(extents=[.008, .008, .0008])
            inner = trimesh.creation.box(extents=[.004, .004, .0004])
            trimesh.util.concatenate([outer, inner]).export(Path(directory) / 'nested.stl')
            self.geometry['regions'] = [{'id': 'nested', 'material': 'a', 'kind': 'stl',
                'path': 'nested.stl', 'source_unit': 'm',
                'transform': {'scale': 1, 'translation_m': [.005, .005, .0005]}}]
            with self.assertRaises(ValueError):
                self.module.audit_geometry(self.geometry, Path(directory))


if __name__ == '__main__':
    unittest.main()
