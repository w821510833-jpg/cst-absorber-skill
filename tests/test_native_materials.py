"""Offline material-query regressions using the existing single-case fixture."""
import builtins
import copy
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_native_model import case_fixture, model_text


OPERATION = 'offline-material-query-0001'


def material_text(case, *, sigma=('0', '0', '0'), rho='1000'):
    return '\n'.join([
        'schema\tnative-materials-v1',
        'case_signature\t' + case['signature'], 'operation_id\t' + OPERATION,
        'shape_count\t1', 'unit\tconductivity\tS/m', 'unit\tdensity\tkg/m3',
        'unit_source\tconductivity\tMaterial.Sigma/SigmaX/SigmaY/SigmaZ',
        'unit_source\tdensity\tMaterial.Rho',
        'source\tmaterial_binding\tSolid.GetMaterialNameForShape',
        'source\tconductivity_xyz\tMaterial.GetSigma',
        'source\tdensity\tMaterial.GetRho',
        '\t'.join(['material', 'absorber:region_layer', 'mat_a', *sigma, rho]),
    ]) + '\n'


class NativeMaterialTests(unittest.TestCase):
    def setUp(self):
        try:
            self.module = importlib.import_module('cst_absorber.native_materials')
        except ModuleNotFoundError:
            self.module = None
        self.assertIsNotNone(self.module, 'formal material getter generation and report parser must exist')
        self.case = case_fixture()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        from cst_absorber.native_model import read_model_report
        (self.root / 'native_model.tsv').write_text(model_text(), encoding='utf-8')
        self.model = read_model_report(self.case, self.root)

    def read(self, text=None, *, model=None, operation_id=OPERATION):
        (self.root / 'native_materials.tsv').write_text(
            material_text(self.case) if text is None else text, encoding='utf-8')
        return self.module.read_material_report(
            self.case, self.root, self.model if model is None else model,
            operation_id=operation_id)

    def test_formal_queries_bind_actual_material_and_never_guess_getters(self):
        text = self.module.material_readback_vba(self.case, self.root, operation_id=OPERATION)
        self.assertIn('actualMaterial = Solid.GetMaterialNameForShape("absorber:region_layer")', text)
        self.assertIn('Material.GetSigma actualMaterial, sigmaX, sigmaY, sigmaZ', text)
        self.assertIn('Material.GetRho actualMaterial, rho', text)
        self.assertIn(self.case['signature'], text)
        self.assertIn(OPERATION, text)
        self.assertNotIn('GetDistanceToReferencePlane', text)
        self.assertNotIn('GetTDCompatibleMaterials', text)
        self.assertNotIn('GetEpsilon', text)
        self.assertNotIn('GetMu', text)
        self.assertNotIn('On Error Resume Next', text)
        self.assertNotIn('@@ARTIFACT_ROOT@@', text)

    def test_parameter_matches_preserve_raw_xyz_and_density_without_solver_acceptance(self):
        result = self.read()
        self.assertEqual(result['status'], 'material_report_validated')
        self.assertEqual(result['case_signature'], self.case['signature'])
        self.assertEqual(result['operation_id'], OPERATION)
        material = result['materials'][0]
        self.assertEqual(material['shape'], 'absorber:region_layer')
        self.assertEqual(material['actual_material_name'], 'mat_a')
        self.assertEqual(material['conductivity']['raw_xyz'], [0.0, 0.0, 0.0])
        self.assertEqual(material['density']['raw'], 1000.0)
        self.assertEqual(material['density']['unit'], 'kg/m3')
        self.assertEqual(material['density']['unit_source']['method'], 'Material.Rho')
        self.assertTrue(material['density']['unit_documented'])
        self.assertEqual(material['density']['value_kg_m3'], 1000.0)
        self.assertEqual(result['parameter_evidence'], 'readback_consistency_validated')
        self.assertFalse(result['producer_verified'])
        self.assertFalse(result['solver_response_linkage_verified'])
        self.assertFalse(result['numerically_qualified'])
        self.assertFalse(result['physical_accepted'])
        self.assertEqual(result['native_validation'], 'not_established')
        self.assertEqual(result['native_acceptance'], 'not_run')
        self.assertIsNotNone(json.dumps(result, allow_nan=False))

    def test_folded_source_conductivity_does_not_replace_native_zero_requirement(self):
        self.case['material_samples']['a']['source_conductivity_S_m'] = 1.0
        result = self.read()
        self.assertEqual(result['materials'][0]['conductivity']['expected_xyz_S_m'], [0.0] * 3)
        with self.assertRaisesRegex(ValueError, 'conductivity'):
            self.read(material_text(self.case, sigma=('1', '1', '1')))

    def test_every_sigma_axis_rejects_nonzero_and_nonfinite_values(self):
        for axis in range(3):
            for value in ('1e-30', '-1e-30', 'nan', 'inf', 'True'):
                sigma = ['0'] * 3
                sigma[axis] = value
                with self.subTest(axis=axis, value=value), self.assertRaises(ValueError):
                    self.read(material_text(self.case, sigma=sigma))

    def test_density_mismatch_and_nonfinite_values_fail(self):
        for rho in ('1001', '0', '-1', 'nan', 'inf', 'True'):
            with self.subTest(rho=rho), self.assertRaises(ValueError):
                self.read(material_text(self.case, rho=rho))
        result = self.read(material_text(self.case, rho='1000.0004'))
        self.assertTrue(result['materials'][0]['density']['matches_requested'])

    def test_unrequested_density_is_observed_without_default_expectation(self):
        self.case['material_samples']['a']['density_kg_m3'] = None
        result = self.read(material_text(self.case, rho='0'))
        self.assertEqual(result['materials'][0]['density']['status'], 'observed_unrequested')
        self.assertIsNone(result['materials'][0]['density']['expected_kg_m3'])
        self.assertIsNone(result['materials'][0]['density']['matches_requested'])
        self.assertFalse(result['materials'][0]['density']['mass_qualified'])

    def test_mismatched_actual_shape_material_case_and_nonce_are_rejected(self):
        variants = [material_text(self.case).replace('mat_a', 'mat_foreign'),
                    material_text(self.case).replace('absorber:region_layer', 'absorber:region_foreign'),
                    material_text(self.case).replace(self.case['signature'], '0' * 64),
                    material_text(self.case).replace(OPERATION, 'previous-query'),
                    material_text(self.case).replace('shape_count\t1', 'shape_count\t2')]
        for text in variants:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.read(text)

    def test_model_actual_material_and_shape_identity_are_required(self):
        for change in ('unvalidated', 'material', 'shape', 'duplicate', 'signature'):
            model = copy.deepcopy(self.model)
            if change == 'unvalidated':
                model['status'] = 'unresolved'
            elif change == 'material':
                model['shapes'][0]['material'] = 'mat_foreign'
            elif change == 'shape':
                model['shapes'][0]['name'] = 'absorber:region_foreign'
            elif change == 'duplicate':
                model['shapes'].append(copy.deepcopy(model['shapes'][0]))
            else:
                model['case_signature'] = '0' * 64
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.read(model=model)

    def test_missing_duplicate_unknown_and_truncated_records_fail(self):
        base = material_text(self.case)
        variants = [base.replace('source\tdensity\tMaterial.GetRho\n', ''),
                    base + 'operation_id\t' + OPERATION + '\n',
                    base + 'material\tabsorber:region_layer\tmat_a\t0\t0\t0\t1000\n',
                    base + 'guessed_getter\tpassed\n',
                    base.replace('mat_a\t0\t0\t0\t1000', 'mat_a\t0\t0'),
                    base.replace('Material.GetSigma', 'Material.GetEpsilon'),
                    base.replace('kg/m3', 'unknown'),
                    base.replace('Material.Rho', 'configured_density')]
        for text in variants:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.read(text)

    def test_invalid_case_signature_nonce_and_identifier_fail_before_generation(self):
        for nonce in ('', 'bad\nnonce', 'bad\tnonce', 'bad"nonce', 'bad/nonce'):
            with self.subTest(nonce=nonce), self.assertRaises(ValueError):
                self.module.material_readback_vba(self.case, self.root, operation_id=nonce)
        for field, value in (('signature', ''), ('id', 'layer\nforeign')):
            case = copy.deepcopy(self.case)
            if field == 'signature':
                case[field] = value
            else:
                case['geometry']['regions'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.module.material_readback_vba(case, self.root, operation_id=OPERATION)

    def test_alias_file_and_oversize_content_are_rejected(self):
        report = self.root / 'native_materials.tsv'
        report.write_text(material_text(self.case), encoding='utf-8')
        alias = self.root / 'alias.tsv'
        os.link(report, alias)
        with self.assertRaisesRegex(ValueError, 'alias|link'):
            self.module.read_material_report(self.case, self.root, self.model, operation_id=OPERATION)
        alias.unlink()
        with report.open('wb') as stream:
            stream.truncate(8 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, 'bound|limit'):
            self.module.read_material_report(self.case, self.root, self.model, operation_id=OPERATION)

    def test_file_mutation_during_read_is_rejected(self):
        self.read()
        original = os.fstat
        calls = 0
        def changed(fd):
            nonlocal calls
            calls += 1
            value = original(fd)
            if calls == 2:
                class Changed:
                    def __getattr__(self, key):
                        return getattr(value, key)
                    st_mtime_ns = value.st_mtime_ns + 1
                return Changed()
            return value
        with patch('cst_absorber.native_materials.os.fstat', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'changed|stable'):
                self.module.read_material_report(self.case, self.root, self.model, operation_id=OPERATION)

    def test_root_ancestor_reparse_alias_is_rejected(self):
        self.read()
        original = Path.lstat
        alias_parent = self.root.parent
        def marked_alias(path, *args, **kwargs):
            value = original(path, *args, **kwargs)
            if path == alias_parent:
                class ReparseDirectory:
                    def __getattr__(self, key):
                        return getattr(value, key)
                    st_file_attributes = getattr(value, 'st_file_attributes', 0) | 0x400
                return ReparseDirectory()
            return value
        # Reparse metadata is injected because Windows junction creation may
        # require unavailable privileges; all report data remains real.
        with patch.object(Path, 'lstat', marked_alias):
            with self.assertRaisesRegex(ValueError, 'alias|link|reparse'):
                self.module.read_material_report(self.case, self.root, self.model, operation_id=OPERATION)

    def test_quoted_multiline_fields_cannot_normalize_into_matching_values(self):
        base = material_text(self.case)
        for old, new in [('1000\n', '"10\n00"\n'),
                         (OPERATION, '"offline-material-\nquery-0001"'),
                         ('mat_a', '"mat_\na"'),
                         ('absorber:region_layer', '"absorber:region_\nlayer"')]:
            with self.subTest(field=old), self.assertRaises(ValueError):
                self.read(base.replace(old, new))

    def test_unsupported_policy_and_reference_getters_remain_null(self):
        result = self.read()
        for key in ('fd_material_policy_readback', 'fd_material_source_readback', 'reference_plane_readback'):
            observed = result['readback_limitations'][key]
            self.assertEqual(observed['status'], 'unsupported_public_getter')
            self.assertIsNone(observed['observed_value'])
            self.assertIsNone(observed['getter'])
        self.assertIn('material_solver_response_linkage', result['unresolved_gates'])
        self.assertIn('reference_plane_readback', result['unresolved_gates'])

    def test_missing_invalid_utf8_and_nul_reports_fail_closed(self):
        with self.assertRaises(ValueError):
            self.module.read_material_report(self.case, self.root, self.model, operation_id=OPERATION)
        for data in (b'\xff', material_text(self.case).encode() + b'\x00'):
            (self.root / 'native_materials.tsv').write_bytes(data)
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.module.read_material_report(self.case, self.root, self.model, operation_id=OPERATION)

    def test_queries_and_parser_do_not_import_or_use_cst(self):
        original = builtins.__import__
        def forbid(name, *args, **kwargs):
            if name == 'cst' or name.startswith('cst.'):
                raise AssertionError('offline material module cannot import CST')
            return original(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=forbid):
            self.module.material_readback_vba(self.case, self.root, operation_id=OPERATION)
            self.read()


if __name__ == '__main__':
    unittest.main()
