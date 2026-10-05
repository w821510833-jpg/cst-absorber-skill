"""Executable adapter control-flow tests; no vendor import or process query."""
import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from cst_absorber.contracts import build_plan
try:
    from cst_absorber.live_backend import ExecutableCstBackend, NativeRunStopped
except ImportError:
    ExecutableCstBackend = None


class ModelFixture:
    """Injected readback fixture; it does not claim CST accepted these values."""
    def __init__(self, gates=None, fail_model=False, fail_mesh=False, mesher_threads=2):
        self.gates = gates if gates is not None else ['reference_plane_readback']
        self.fail_model, self.fail_mesh = fail_model, fail_mesh
        self.mesher_threads=mesher_threads
    def bind_artifact_text(self, text, root):
        return text.replace('@@ARTIFACT_ROOT@@', str(root).replace('\\', '/'))
    def model_readback_vba(self, case, root): return 'READ_MODEL'
    def read_model_report(self, case, root):
        if self.fail_model: raise ValueError('actual material map mismatch')
        return {'status':'model_report_validated', 'unresolved_gates':self.gates,
                'modes_port':'Zmax', 'modes':[{'index':1,'name':'TE(0,0)','m':0,'n':0,'polarization':'TE','port':'Zmax'}]}
    def resource_mesh_vba(self, case, runtime, report): return 'APPLY_CPU_MESH'
    def mesh_readback_vba(self, root): return 'READ_MESH'
    def read_mesh_report(self, case, root, report):
        if self.fail_mesh: raise ValueError('maximum generated edge exceeds requested bound')
        return {'status':'mesh_report_checked', 'unresolved_gates':[],
                'mesher_threads':self.mesher_threads}


class SessionFixture:
    evidence_kind = 'injected_test_interface'
    def __init__(self, root, *, solver_state='SUCCESS', fail_stage=None,
                 closed=True, running=None, stop_on_start=None):
        self.project_path = Path(root)/'model.cst'
        self.project_path.parent.mkdir(parents=True,exist_ok=True)
        self.calls=[]; self.fail_stage=fail_stage; self.closed=closed
        self.solver_state=solver_state; self.running=list(running or [False])
        self.stop_on_start=stop_on_start
        self.session={'session_id':'fake-owned','pid':123,'created_at':'fake-created',
                      'executable':str(Path(root)/'fake-cst.exe'),'run_dir':str(Path(root).resolve())}
    def action(self, name):
        self.calls.append(name)
        if self.fail_stage==name: raise RuntimeError('injected '+name+' failure')
    def save(self, include_results=True, timeout_seconds=1):
        self.action('save'); self.project_path.write_bytes(b'FAKE_INTERFACE_FIXTURE_NOT_CST')
    def add_to_history(self, header, code, timeout_seconds=1): self.action('history:'+header)
    def execute_vba(self, code, timeout_seconds=1): self.action('vba:'+code)
    def start_solver(self, timeout_seconds=1):
        self.action('start')
        if self.stop_on_start: self.stop_on_start.set()
    def is_solver_running(self, timeout_seconds=1):
        self.action('poll')
        return self.running.pop(0) if self.running else False
    def get_solver_run_info(self, timeout_seconds=1):
        self.action('run_info'); return {'state':self.solver_state,'fixture':True}
    def abort_solver(self, timeout_seconds=1): self.action('abort')
    def close_project(self, timeout_seconds=1): self.action('project_close')
    def close(self, timeout_seconds=1):
        self.action('close'); return {'owned_closed':self.closed}
    def ownership(self): return copy.deepcopy(self.session),copy.deepcopy(self.session)
    def cleanup_identity(self, record): return self.close()


class ResultFixture:
    def __init__(self, session, status='completed', mapping_status='diagnostic_only', gates=None):
        self.session,self.status,self.mapping_status=session,status,mapping_status
        self.gates=gates if gates is not None else ['material_fit_readback']
    def export_raw_results(self, project_path, output_dir, **kwargs):
        self.session.action('export_raw')
        self.assert_project_closed = 'project_close' in self.session.calls
        output_dir=Path(output_dir); output_dir.mkdir(parents=True,exist_ok=True)
        raw={'schema_version':'cst-raw-results/1','status':self.status,
             'validation':'not_run','curves':[], 'errors':[] if self.status=='completed' else ['partial'],
             'artifacts':['resulttree.json']}
        (output_dir/'resulttree.json').write_text(json.dumps(raw),encoding='utf-8')
        return raw
    def canonicalize_raw_results(self, raw, profile, case, model_report, out_dir):
        self.session.action('canonicalize')
        out_dir=Path(out_dir); out_dir.mkdir(parents=True,exist_ok=True)
        if self.mapping_status=='failed':
            return {'status':'failed','errors':['mapping mismatch'],'unresolved_gates':self.gates}
        spectra=out_dir/'spectra.csv'; power=out_dir/'power.csv'
        shutil.copyfile(ROOT/'examples/synthetic_spectra.csv',spectra)
        shutil.copyfile(ROOT/'examples/synthetic_power.csv',power)
        return {'status':'diagnostic_only','spectra_path':str(spectra),'power_path':str(power),
                'unresolved_gates':self.gates,'numerically_qualified':False,
                'transmission_source':'read_back_PEC_boundary_assumption',
                'native_acceptance':'not_run'}


class LiveBackendTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(ExecutableCstBackend,'executable adapter chain is missing')
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        self.case=build_plan(json.loads((ROOT/'examples/single.json').read_text(encoding='utf-8')),
                             ROOT/'examples')['cases'][0]
        self.runtime={'max_cpus':2,'max_modes':16,'wall_budget_seconds':20,
                      'min_available_RAM_GiB':0,'min_free_disk_GiB':0,'max_attempts':1}
        self.attempt=self.root/'attempt'; self.attempt.mkdir()
        (self.attempt/'runtime.json').write_text(json.dumps(self.runtime),encoding='utf-8')
        self.created=[]
    def make(self, model=None, **options):
        self.session_options=options.pop('session_options',{})
        session=SessionFixture(self.attempt,**self.session_options)
        result=options.pop('result_adapter',ResultFixture(session))
        def factory(directory, **kwargs):
            self.created.append((directory,kwargs)); return session
        backend=ExecutableCstBackend(authorized=True,exclusive_resources=True,acceptance_run=True,
                                     session_factory=factory,model_adapter=model or ModelFixture(),
                                     result_adapter=result,results_module=object(),poll_interval_seconds=.001,
                                     **options)
        return backend,session,result
    def test_default_gate_never_creates_session_or_preparation(self):
        called=[]
        worker=ExecutableCstBackend(session_factory=lambda *a,**k:called.append(True))
        receipt=worker(self.case,self.attempt,threading.Event())
        self.assertEqual(receipt['status'],'unsupported_validation')
        self.assertEqual(receipt['CST_execution'],'not_run')
        self.assertTrue(receipt['owned_closed']); self.assertFalse(called)
        self.assertFalse((self.attempt/'native-prepared').exists())
    def test_exclusive_resource_and_output_mode_gates_precede_startup(self):
        for flags in ({'authorized':True}, {'authorized':True,'exclusive_resources':True}):
            with self.subTest(flags=flags):
                worker=ExecutableCstBackend(**flags,session_factory=lambda *a,**k:self.fail('startup'))
                receipt=worker(self.case,self.attempt,threading.Event())
                self.assertEqual(receipt['CST_execution'],'not_run')
                self.assertFalse((self.attempt/'native-prepared').exists())
    def test_actual_chain_builds_solves_saves_closes_project_then_exports(self):
        worker,session,result=self.make()
        receipt=worker(self.case,self.attempt,threading.Event())
        self.assertEqual(receipt['status'],'failed')
        self.assertEqual(receipt['reason_code'],'native_validation_pending')
        self.assertFalse(receipt['retryable']); self.assertTrue(receipt['owned_closed'])
        for step in ['history:units','history:materials','history:geometry','history:setup',
                     'history:cpu_mesh','start','poll','run_info','vba:READ_MESH','export_raw','close']:
            self.assertIn(step,session.calls)
        self.assertLess(session.calls.index('history:units'),session.calls.index('history:geometry'))
        self.assertLess(session.calls.index('project_close'),session.calls.index('export_raw'))
        self.assertTrue(result.assert_project_closed)
        self.assertEqual(session.calls.count('start'),1)
        self.assertEqual(receipt['backend_evidence'],'injected_test_interface')
        self.assertEqual(receipt['CST_execution'],'not_run_injected_test_interface')
        self.assertFalse(receipt['numerically_qualified'])
        self.assertIn('native-run.json',receipt['artifacts'])
    def test_prepared_tamper_rejected_before_any_sdk_creation(self):
        worker,session,_=self.make()
        case=copy.deepcopy(self.case);case['material_samples']['a']['epsilon_real'][0]=999
        receipt=worker(case,self.attempt,threading.Event())
        self.assertFalse(self.created); self.assertTrue(receipt['owned_closed'])
        self.assertEqual(receipt['status'],'failed'); self.assertNotIn('start',session.calls)
    def test_model_readback_mismatch_prevents_solver_and_closes_owned_session(self):
        worker,session,_=self.make(model=ModelFixture(fail_model=True))
        receipt=worker(self.case,self.attempt,threading.Event())
        self.assertEqual(receipt['status'],'failed');self.assertNotIn('start',session.calls)
        self.assertIn('close',session.calls); self.assertTrue(receipt['owned_closed'])
    def test_solver_failure_retains_info_and_does_not_claim_completion(self):
        worker,session,_=self.make(session_options={'solver_state':'FAILED'})
        receipt=worker(self.case,self.attempt,threading.Event())
        self.assertEqual(receipt['status'],'failed');self.assertFalse(receipt['retryable'])
        self.assertIn('close',session.calls)
        self.assertTrue((self.attempt/'solver-info.json').is_file())
        self.assertTrue((self.attempt/'raw-results/resulttree.json').is_file())
        self.assertEqual(receipt['reason_code'],'native_solver_not_success')
    def test_cancelled_solver_is_aborted_only_through_owned_transport(self):
        event=threading.Event()
        worker,session,_=self.make(session_options={'running':[True], 'stop_on_start':event})
        receipt=worker(self.case,self.attempt,event)
        self.assertEqual(receipt['status'],'failed');self.assertIn('abort',session.calls)
        self.assertTrue(receipt['interruption_acknowledged'])
        self.assertTrue(receipt['owned_closed']);self.assertNotIn('export_raw',session.calls)
    def test_unconfirmed_closure_never_returns_complete_or_retries_cleanup(self):
        worker,session,_=self.make(session_options={'closed':False})
        receipt=worker(self.case,self.attempt,threading.Event())
        self.assertFalse(receipt['owned_closed']);self.assertNotEqual(receipt['status'],'completed')
        recorded,live=worker.ownership(self.attempt)
        self.assertEqual(recorded['pid'],live['pid'])
        self.assertFalse(worker.cleanup_identity(recorded)['owned_closed'])
        self.assertEqual(session.calls.count('close'),1)
    def test_typed_sdk_cancel_and_safety_failure_have_distinct_pause_acknowledgement(self):
        from cst_absorber.native_sdk import SessionCancelled, SessionSafetyError
        for label,error,acknowledged in [('cancel',SessionCancelled('caller cancelled'),True),
                                        ('identity',SessionSafetyError('foreign identity'),False),
                                        ('budget',NativeRunStopped('wall budget exhausted'),False)]:
            with self.subTest(label=label):
                self.attempt=self.root/label
                self.attempt.mkdir()
                (self.attempt/'runtime.json').write_text(json.dumps(self.runtime),encoding='utf-8')
                event=threading.Event()
                worker,session,_=self.make()
                original=session.action
                def action(name):
                    original(name)
                    if name=='history:geometry':
                        event.set()
                        raise error
                session.action=action
                receipt=worker(self.case,self.attempt,event)
                self.assertEqual(receipt['interruption_acknowledged'],acknowledged)
                self.assertTrue(receipt['owned_closed'])
                self.assertEqual(receipt['status'],'failed')
    def test_partial_native_tree_is_preserved_and_mapping_not_attempted(self):
        worker,session,result=self.make(result_profile={'fixture':True})
        result.status='partial'
        receipt=worker(self.case,self.attempt,threading.Event())
        self.assertEqual(receipt['status'],'failed');self.assertNotIn('canonicalize',session.calls)
        self.assertTrue((self.attempt/'raw-results/resulttree.json').is_file())
    def test_mesh_validation_failure_still_preserves_finished_solver_raw_results(self):
        worker,session,_=self.make(model=ModelFixture(fail_mesh=True),result_profile={'fixture':True})
        receipt=worker(self.case,self.attempt,threading.Event())
        self.assertEqual(receipt['status'],'failed')
        self.assertEqual(receipt['reason_code'],'native_mesh_validation_failed')
        self.assertTrue((self.attempt/'raw-results/resulttree.json').is_file())
        self.assertIn('save',session.calls[session.calls.index('run_info')+1:])
        self.assertNotIn('canonicalize',session.calls)
        self.assertTrue(receipt['owned_closed'])
        self.assertFalse(receipt['interruption_acknowledged'])
    def test_actual_mesher_threads_are_bound_to_controller_runtime_snapshot(self):
        worker,session,_=self.make(model=ModelFixture(mesher_threads=99))
        self.assertNotIn('runtime',self.case)
        receipt=worker(self.case,self.attempt,threading.Event())
        self.assertEqual(receipt['reason_code'],'native_mesh_validation_failed')
        self.assertTrue((self.attempt/'raw-results/resulttree.json').is_file())
        self.assertTrue(receipt['owned_closed'])
    def test_explicit_mapping_runs_existing_analysis_and_plot_with_pending_gates(self):
        worker,session,_=self.make(result_profile={'fixture':True})
        receipt=worker(self.case,self.attempt,threading.Event())
        self.assertIn('canonicalize',session.calls)
        self.assertEqual(receipt['reason_code'],'native_validation_pending')
        metrics=json.loads((self.attempt/'analysis/metrics.json').read_text(encoding='utf-8'))
        self.assertAlmostEqual(metrics['metrics']['average_A'],.96)
        self.assertFalse(metrics['numerically_qualified'])
        self.assertEqual(metrics['CST_execution'],'not_run_injected_test_interface')
        self.assertEqual(metrics['provenance']['export_origin'],'injected_fixture_project')
        self.assertEqual(metrics['provenance']['transmission_source'],'read_back_PEC_boundary_assumption')
        self.assertFalse(metrics['provenance']['transmission_exported'])
        self.assertTrue((self.attempt/'analysis/reflection_loss.png').is_file())
    def test_startup_exception_with_unknown_session_keeps_closure_unconfirmed(self):
        def bad_factory(*args,**kwargs): raise TimeoutError('new environment did not reply')
        worker=ExecutableCstBackend(authorized=True,exclusive_resources=True,acceptance_run=True,
                                     session_factory=bad_factory,model_adapter=ModelFixture())
        receipt=worker(self.case,self.attempt,threading.Event())
        self.assertFalse(receipt['owned_closed']);self.assertEqual(receipt['session_created'],'unknown')
    def test_stop_before_creation_does_not_submit_or_open_anything(self):
        event=threading.Event();event.set()
        worker,session,_=self.make()
        receipt=worker(self.case,self.attempt,event)
        self.assertFalse(self.created);self.assertEqual(receipt['CST_execution'],'not_run')
        self.assertTrue(receipt['owned_closed'])


if __name__=='__main__': unittest.main()
