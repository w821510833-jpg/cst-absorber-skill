"""Offline preparation keeps nominal mesh requests distinct from acceptance."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from cst_absorber.backend import get_capabilities, prepare_cst
from cst_absorber.contracts import canonical_hash
import test_backend as fixtures


class PreparationMeshPolicyTests(unittest.TestCase):
    def prepare(self, mesh):
        case = fixtures.prepared_case()
        case['mesh'] = copy.deepcopy(mesh)
        case = fixtures.rebuild_fixture(case)
        original = copy.deepcopy(case)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            try:
                receipt = prepare_cst(case, root)
            except ValueError as error:
                self.fail('contract-valid mesh policy must prepare: ' + str(error))
            expected = json.loads((root / 'expected_geometry.json').read_text())
            acceptance = json.loads((root / 'acceptance_requirements.json').read_text())
            setup = (root / 'setup.vba').read_text()
        self.assertEqual(case, original, 'preparation must not rewrite the physical mesh policy')
        self.assertEqual(receipt['case_signature'], original['signature'])
        return case, expected, acceptance, setup

    def test_legacy_mesh_keeps_original_case_hash_and_measured_ceiling(self):
        original = fixtures.prepared_case()
        case, expected, _, _ = self.prepare({'max_edge_m': .0005})
        self.assertEqual(case['signature'], original['signature'])
        self.assertEqual(canonical_hash(case), canonical_hash(original))
        self.assertIn('mesh_policy', expected, 'mesh preparation must expose nominal and acceptance roles')
        mesh = expected['mesh_policy']
        self.assertEqual(mesh['target_edge_m'], .0005)
        self.assertEqual(mesh['acceptance_max_edge_m'], .0005)
        self.assertEqual(mesh['policy_origin'], 'legacy_max_edge_m')
        self.assertTrue(mesh['measurement_acceptance_required'])

    def test_nominal_target_alone_does_not_implicitly_request_a_measured_ceiling(self):
        _, expected, _, _ = self.prepare({'target_edge_m': .0005})
        self.assertIn('mesh_policy', expected, 'nominal mesh preparation must expose a mesh policy')
        mesh = expected['mesh_policy']
        self.assertEqual(mesh['target_edge_m'], .0005)
        self.assertIsNone(mesh['acceptance_max_edge_m'])
        self.assertFalse(mesh['measurement_acceptance_required'])
        self.assertEqual(mesh['policy_origin'], 'explicit_target_edge_m')

    def test_explicit_measured_ceiling_remains_independent_of_nominal_target(self):
        _, expected, _, _ = self.prepare({'target_edge_m': .0005, 'acceptance_max_edge_m': .0007})
        self.assertIn('mesh_policy', expected, 'explicit acceptance must be separately represented')
        mesh = expected['mesh_policy']
        self.assertEqual(mesh['target_edge_m'], .0005)
        self.assertEqual(mesh['acceptance_max_edge_m'], .0007)
        self.assertTrue(mesh['measurement_acceptance_required'])
        self.assertEqual(mesh['measurement_acceptance_status'], 'not_run')

    def test_preparation_does_not_claim_a_native_hard_upper_bound(self):
        _, expected, _, setup = self.prepare({'max_edge_m': .0005})
        self.assertIn('mesh_policy', expected, 'mesh metadata must distinguish a target from a guarantee')
        mesh = expected['mesh_policy']
        self.assertEqual(mesh['sizing_semantics'], 'nominal_unstructured_cell_target')
        self.assertFalse(mesh['native_hard_upper_bound_guaranteed'])
        self.assertFalse(mesh['measurement_units_verified'])
        self.assertFalse(mesh['measurement_freshness_verified'])
        self.assertNotIn('mesh_max_edge_m', expected)
        self.assertIn('Nominal mesh sizing', setup)
        self.assertNotIn('enforcement requires native API validation', setup)

    def test_fd_policy_request_does_not_claim_documented_volumetric_interpolation(self):
        _, _, acceptance, setup = self.prepare({'max_edge_m': .0005})
        self.assertIn('.TDCompatibleMaterials "False"', setup)
        policy = acceptance['material_solver_policy']
        self.assertFalse(policy['requested_TDCompatibleMaterials'])
        self.assertEqual(policy['documented_table_treatment'], 'volumetric_response_undocumented')
        self.assertFalse(policy['native_setting_verified'])
        self.assertEqual(policy['documented_setting_scope'],
                         ['constant_tangent_delta_materials', 'broadband_surface_impedance_materials'])
        self.assertEqual(policy['native_setting_readback'], 'unavailable_public_getter')
        self.assertEqual(policy['solver_response_linkage'], 'unverified')
        self.assertFalse(acceptance['material_response_source_roles']['roles_interchangeable'])

    def test_capability_evidence_records_failed_022_trial_and_unrun_candidate(self):
        capabilities = get_capabilities()
        self.assertEqual(capabilities['native_acceptance'], 'not_run')
        self.assertFalse(capabilities['live_supported'])
        blockers = capabilities['blocked_capabilities']
        self.assertIn('0.2.2', blockers['owned_session_lifecycle'])
        self.assertIn('automatic owned closure', blockers['owned_session_lifecycle'])
        self.assertIn('solver SUCCESS', blockers['owned_session_lifecycle'])
        self.assertIn('archive and mesh', blockers['owned_session_lifecycle'])
        self.assertIn('current candidate has not been rerun', blockers['owned_session_lifecycle'])
        self.assertIn('single synthetic brick', blockers['native_geometry_readback'])
        self.assertIn('nominal', blockers['native_mesh_max_edge'])


if __name__ == '__main__':
    unittest.main()
