"""Root-cause integration without importing any vendor library."""
import hashlib
import json
from pathlib import Path
import sys
import threading
import unittest
import test_native_materials as material_fixtures
import test_native_model as model_fixtures

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import test_live_backend as fixtures


class OversizeModel(fixtures.ModelFixture):
    def read_mesh_report(self, case, root, report):
        return {'status':'mesh_report_checked_with_unit_assumption', 'mesher_threads':2,
                'measurement_acceptance_failed':True, 'measurement_acceptance_required':True,
                'max_edge_m_under_assumption':0.00116412, 'acceptance_max_edge_m':0.001,
                'unresolved_gates':['mesh_edge_unit','mesh_edge_freshness']}


class CompletionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.LiveBackendTests('test_default_gate_never_creates_session_or_preparation')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def completion(self, session):
        def run(timeout_seconds=1):
            session.action('run_solver_complete')
            session.save(timeout_seconds=timeout_seconds)
            return {'completion_method':'Model3D.run_solver','operation_id':'b'*32,
                    'solver_info':{'state':'SUCCESS'},
                    'archive_integrity':{'status':'pinned','archive_integrity_verified':True,
                        'sha256':hashlib.sha256(session.project_path.read_bytes()).hexdigest()},
                    'write_trace':[]}
        session.run_solver_and_snapshot = run

    def test_acceptance_uses_one_documented_completion_dispatch(self):
        f=self.fixture; worker,session,result=f.make()
        self.completion(session)
        receipt=worker(f.case,f.attempt,threading.Event())
        self.assertEqual(session.calls.count('run_solver_complete'),1)
        self.assertNotIn('start',session.calls)
        self.assertIn('export_raw',session.calls)
        self.assertTrue((f.attempt/'solver-binding.json').exists())
        self.assertFalse(receipt['numerically_qualified'])

    def test_oversize_mesh_report_persisted_before_failed_acceptance(self):
        f=self.fixture; worker,session,result=f.make(model=OversizeModel())
        self.completion(session)
        receipt=worker(f.case,f.attempt,threading.Event())
        self.assertEqual(receipt['reason_code'],'native_mesh_validation_failed')
        self.assertTrue((f.attempt/'mesh-readback.json').exists())
        report=json.loads((f.attempt/'mesh-readback.json').read_text(encoding='utf-8'))
        self.assertTrue(report['measurement_acceptance_failed'])
        self.assertIn('export_raw',session.calls)
        self.assertEqual(session.calls.count('run_solver_complete'),1)
        self.assertFalse(receipt['numerically_qualified'])

    def test_completion_receives_remaining_work_budget_not_short_read_budget(self):
        f=self.fixture; worker,session,result=f.make(call_timeout_seconds=.01)
        self.completion(session)
        original=session.run_solver_and_snapshot; timeouts=[]
        def observe(timeout_seconds=1):
            timeouts.append(timeout_seconds)
            return original(timeout_seconds=timeout_seconds)
        session.run_solver_and_snapshot=observe
        worker(f.case,f.attempt,threading.Event())
        self.assertGreater(timeouts[0],worker.call_timeout_seconds)
        self.assertLessEqual(timeouts[0],f.runtime['wall_budget_seconds'])

    def test_actual_acceptance_transport_without_completion_cannot_fall_back_to_async(self):
        f=self.fixture; worker,session,result=f.make()
        session.evidence_kind='native_sdk'
        receipt=worker(f.case,f.attempt,threading.Event())
        self.assertEqual(receipt['status'],'failed')
        self.assertNotIn('start',session.calls)
        self.assertFalse(receipt['numerically_qualified'])

    def material_setup(self, *, receipt_hash_valid=True):
        f=self.fixture; f.case=model_fixtures.case_fixture()
        class MaterialModel(fixtures.ModelFixture):
            def read_model_report(inner,case,root):
                from cst_absorber.native_model import read_model_report
                (root/'native_model.tsv').write_text(model_fixtures.model_text(),encoding='utf-8')
                return read_model_report(case,root)
        worker,session,result=f.make(model=MaterialModel())
        self.completion(session)
        def produce(case,*,operation_id,timeout_seconds=1):
            session.action('read_material_parameters')
            text=material_fixtures.material_text(case).replace(material_fixtures.OPERATION,operation_id)
            output=f.attempt/'native_materials.tsv'; output.write_text(text,encoding='utf-8')
            return {'operation_id':operation_id,
                    'source_sha256':hashlib.sha256(output.read_bytes()).hexdigest() if receipt_hash_valid else '0'*64}
        session.execute_material_readback=produce
        return f,worker,session

    def test_formal_material_observation_is_bound_without_native_response_claim(self):
        f,worker,session=self.material_setup()
        receipt=worker(f.case,f.attempt,threading.Event())
        report=json.loads((f.attempt/'material-readback.json').read_text(encoding='utf-8'))
        self.assertEqual(report['operation_id'],'b'*32)
        self.assertEqual(report['solver_binding']['execution_operation_id'],'b'*32)
        self.assertFalse(report['producer_verified'])
        self.assertFalse(report['solver_binding']['native_solver_response_linkage_verified'])
        self.assertEqual(report['materials'][0]['conductivity']['value_xyz_S_m'],[0.0]*3)
        self.assertIn('fd_material_policy_readback',receipt['unresolved_gates'])
        self.assertIn('reference_plane_readback',receipt['unresolved_gates'])

    def test_material_output_hash_must_match_owned_request_receipt(self):
        f,worker,session=self.material_setup(receipt_hash_valid=False)
        receipt=worker(f.case,f.attempt,threading.Event())
        self.assertEqual(receipt['status'],'failed')
        self.assertFalse((f.attempt/'material-readback.json').exists())
        self.assertTrue(any('owned SDK operation output' in e['message'] for e in receipt['errors']))

    def test_failed_completion_retains_actual_stopped_info_without_abort_or_repin(self):
        from cst_absorber.native_sdk import SessionSafetyError
        f=self.fixture; worker,session,result=f.make()
        def failed(timeout_seconds=1):
            session.action('run_solver_complete')
            error=SessionSafetyError('blocking completion requires SUCCESS')
            error.completed_observation={'completion_method':'Model3D.run_solver','operation_id':'b'*32,
                                         'solver_running':False,'solver_info':{'state':'FAILED','message':'fixture-only'}}
            raise error
        session.run_solver_and_snapshot=failed
        receipt=worker(f.case,f.attempt,threading.Event())
        info_path=f.attempt/'solver-info.json'
        self.assertTrue(info_path.exists(),'actual stopped failure info must be retained')
        self.assertEqual(json.loads(info_path.read_text(encoding='utf-8'))['info']['state'],'FAILED')
        self.assertNotIn('abort',session.calls)
        self.assertFalse((f.attempt/'archive-completion.json').exists())
        self.assertFalse(receipt['numerically_qualified'])

    def test_native_label_without_owned_material_receipt_cannot_verify_producer(self):
        f,worker,session=self.material_setup()
        session.evidence_kind='native_sdk'
        receipt=worker(f.case,f.attempt,threading.Event())
        path=f.attempt/'material-readback.json'
        self.assertFalse(path.exists(),'transport label alone cannot authenticate material output')
        self.assertEqual(receipt['status'],'failed')
        self.assertFalse(receipt['numerically_qualified'])

    def test_failed_sdk_stage_persists_cached_write_trace_without_acceptance(self):
        f=self.fixture; worker,session,result=f.make()
        def failed(timeout_seconds=1):
            session.action('run_solver_complete')
            raise RuntimeError('injected SDK exception')
        session.run_solver_and_snapshot=failed
        events=[{'operation_id':'b'*32,'stage':'run_solver','phase':'after','archive_status':'writing'}]
        session.archive_write_trace=lambda:events
        receipt=worker(f.case,f.attempt,threading.Event())
        trace_path=f.attempt/'archive-write-trace.json'
        self.assertTrue(trace_path.exists(),'failed SDK call boundary must survive program exit')
        report=json.loads(trace_path.read_text(encoding='utf-8'))
        self.assertEqual(report['events'],events)
        self.assertEqual(report['snapshot_scope'],'cached_observation_at_cleanup_not_completion_proof')
        self.assertFalse(receipt['numerically_qualified'])
        self.assertFalse((f.attempt/'archive-completion.json').exists())


if __name__ == '__main__':
    unittest.main()
