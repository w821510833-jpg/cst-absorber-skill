"""Executable, explicitly admitted CST SDK chain; native acceptance is separate.

The current development task runs this only with injected interfaces. The default
transport imports CST lazily after authorization and exclusive-resource admission.
Neither synthetic tests nor completion of a solver call certify numerical accuracy.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import threading
import time


class NativeRunStopped(RuntimeError):
    def __init__(self, message, *, caller_stop=False):
        super().__init__(message)
        self.caller_stop = caller_stop is True


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False) + '\n', encoding='utf-8')


class ExecutableCstBackend:
    """Runtime worker implementing build/solve/export/closure with a real SDK.

    Explicit admission permits execution, never a certification claim. An
    acceptance run exports actual raw data then returns native_validation_pending.
    A result profile additionally maps actual curves for diagnostic analysis.
    The instance is used serially and retains its one owned transport for cleanup.
    """
    def __init__(self, *, authorized=False, exclusive_resources=False,
                 acceptance_run=False, result_profile=None, session_factory=None,
                 results_module=None, model_adapter=None, result_adapter=None,
                 poll_interval_seconds=.2, call_timeout_seconds=30,
                 cleanup_timeout_seconds=10):
        self.authorized = authorized is True
        self.exclusive_resources = exclusive_resources is True
        self.acceptance_run = acceptance_run is True
        self.result_profile = copy.deepcopy(result_profile)
        self.session_factory, self.results_module = session_factory, results_module
        self.model_adapter, self.result_adapter = model_adapter, result_adapter
        for value in (poll_interval_seconds, call_timeout_seconds, cleanup_timeout_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError('adapter timing settings must be finite positive seconds')
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.call_timeout_seconds = float(call_timeout_seconds)
        self.cleanup_timeout_seconds = float(cleanup_timeout_seconds)
        self._session = None
        self._attempt = None
        self._close_attempted = False
        self.last_receipt = None

    def ownership(self, attempt_dir):
        if self._session is None or self._attempt is None or Path(attempt_dir).resolve() != self._attempt:
            return {}, {}
        try:
            recorded, live = self._session.ownership()
            return recorded or {}, live or {}
        except Exception:
            return {}, {}

    def cleanup_identity(self, recorded):
        if self._session is None or self._close_attempted:
            return {'owned_closed': False, 'reason': 'closure action absent or already attempted'}
        current, live = self.ownership(self._attempt)
        from .runtime import verify_owned
        if current != recorded or not verify_owned(recorded, live, self._attempt):
            return {'owned_closed': False, 'reason': 'owned identity mismatch'}
        self._close_attempted = True
        return self._session.cleanup_identity(recorded)

    def _denied(self, case, reason):
        from .backend import get_capabilities
        self.last_receipt = {'status':'unsupported_validation', 'reason':reason,
                             'CST_execution':'not_run', 'session_created':False,
                             'owned_closed':True, 'artifacts':[], 'retryable':False,
                             'case_id':case.get('id'), 'backend':'cst',
                             'backend_evidence':'not_run', 'validation':'not_run',
                             'native_acceptance':'not_run', 'numerically_qualified':False,
                             'physical_certification':False,
                             'blocked_capabilities':get_capabilities()['blocked_capabilities']}
        return copy.deepcopy(self.last_receipt)

    def __call__(self, case, run_dir, stop_event=None):
        if not self.authorized or not self.exclusive_resources:
            return self._denied(case, 'Explicit execution authorization and exclusive resources are required.')
        if not self.acceptance_run and self.result_profile is None:
            return self._denied(case, 'Choose an explicit acceptance run or provide an actual-result mapping profile.')
        event = stop_event if stop_event is not None else threading.Event()
        original_root = Path(run_dir)
        if original_root.is_symlink():
            raise ValueError('native attempt directory must not be a symlink')
        root = original_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        if (root/'native-run.json').exists():
            raise ValueError('native run already exists; use a fresh controller attempt')
        self._session, self._attempt, self._close_attempted = None, root, False
        state = {'schema_version':'cst-native-run/1', 'case_id':case.get('id'),
                 'case_signature':case.get('signature'), 'status':'running',
                 'stage':'admission', 'stage_log':[], 'CST_execution':'not_run',
                 'session_created':False, 'backend_evidence':'unknown',
                 'native_acceptance':'not_run', 'numerically_qualified':False,
                 'interruption_acknowledged':False,
                 'physical_certification':False, 'unresolved_gates':[], 'errors':[]}
        deadline = None
        creation_requested = False
        solver_start_requested = False
        solver_finished = False
        session = None
        model_report = None
        raw_export = None
        mapping = None
        status = 'failed'
        reason_code = 'native_validation_pending'
        owned_closed = True

        def checkpoint():
            if event.is_set():
                raise NativeRunStopped('stop_event requested',caller_stop=True)
            if deadline is not None and time.monotonic() >= deadline:
                raise NativeRunStopped('native worker wall budget exhausted')

        def timeout():
            checkpoint()
            return min(self.call_timeout_seconds, max(.001, deadline-time.monotonic()))

        def stage(name):
            state['stage'] = name
            state['stage_log'].append(name)
            _write(root/'native-run.json', state)
            checkpoint()

        def export_raw():
            adapter = self.result_adapter
            if adapter is None:
                from . import native_results as adapter
            results_module = self.results_module
            if results_module is None:
                if state['backend_evidence'] != 'native_sdk':
                    raise ValueError('injected transport requires an injected results module')
                # Only admitted real execution reaches this lazy import.
                import cst.results as results_module
            stage('export_actual_results')
            exported = adapter.export_raw_results(session.project_path,root/'raw-results',
                                                   results_module=results_module,stop_event=event)
            state['raw_results'] = 'raw-results/resulttree.json'
            return adapter,exported

        try:
            checkpoint()
            runtime_path = root/'runtime.json'
            if runtime_path.is_symlink() or not runtime_path.is_file():
                raise ValueError('immutable runtime.json is required from the shared controller')
            runtime = json.loads(runtime_path.read_text(encoding='utf-8'))
            budget = runtime.get('wall_budget_seconds')
            if isinstance(budget, bool) or not isinstance(budget, (int,float)) or not math.isfinite(budget) or budget <= 0:
                raise ValueError('finite positive native worker wall budget required')
            deadline = time.monotonic()+float(budget)
            stage('verify_and_stage')
            from .backend import prepare_cst
            prepared_root = root/'native-prepared'
            preparation = prepare_cst(case, prepared_root)
            state['preparation'] = preparation
            model = self.model_adapter
            if model is None:
                from . import native_model as model
            factory = self.session_factory
            if factory is None:
                from .native_sdk import CstSdkSession
                factory = CstSdkSession
            stage('create_owned_session')
            creation_requested = True
            owned_closed = False
            state['session_created'] = 'unknown'
            try:
                session = factory(root, authorized=True, stop_event=event,
                                  startup_timeout_seconds=timeout())
            except BaseException as error:
                session = getattr(error,'session',None)
                self._session = session
                raise
            self._session = session
            state['session_created'] = True
            state['backend_evidence'] = getattr(session,'evidence_kind','unknown_transport')
            checkpoint()
            stage('save_initial_project')
            session.save(include_results=True, timeout_seconds=timeout())
            # Units must be set before any numerical brick or transformed STL is submitted.
            session.add_to_history('units', 'With Units\n .SetUnit "Length", "mm"\n .SetUnit "Frequency", "GHz"\nEnd With\n',
                                   timeout_seconds=timeout())
            for name in ('materials','geometry','setup'):
                stage('history_'+name)
                text = (prepared_root/(name+'.vba')).read_text(encoding='utf-8')
                code = model.bind_artifact_text(text, prepared_root)
                session.add_to_history(name, code, timeout_seconds=timeout())
            stage('read_model')
            session.execute_vba(model.model_readback_vba(case, root), timeout_seconds=timeout())
            model_report = model.read_model_report(case, root)
            _write(root/'model-readback.json', model_report)
            state['unresolved_gates'] = list(model_report.get('unresolved_gates',[]))
            stage('apply_cpu_mesh')
            code = model.resource_mesh_vba(case, runtime, model_report)
            _write(root/'requested-runtime.json',runtime)
            (root/'cpu-mesh.vba').write_text(code,encoding='utf-8')
            session.add_to_history('cpu_mesh',code,timeout_seconds=timeout())
            session.save(include_results=True, timeout_seconds=timeout())
            stage('start_solver')
            solver_start_requested = True
            state['CST_execution'] = 'solver_start_requested'
            session.start_solver(timeout_seconds=timeout())
            state['CST_execution'] = 'solver_started'
            while True:
                checkpoint()
                if not session.is_solver_running(timeout_seconds=timeout()):
                    solver_finished = True  # Stopped is distinct from SUCCESS.
                    break
                event.wait(min(self.poll_interval_seconds,max(.001,deadline-time.monotonic())))
            stage('read_solver_info')
            info = session.get_solver_run_info(timeout_seconds=timeout())
            _write(root/'solver-info.json',{'info':info, 'state_policy':'exact native state SUCCESS required'})
            if not isinstance(info,dict) or info.get('state') != 'SUCCESS':
                reason_code = 'native_solver_not_success'
                raise ValueError('native solver did not report SUCCESS; actual info retained')
            state['CST_execution'] = 'solver_finished'
            stage('read_generated_mesh')
            try:
                session.execute_vba(model.mesh_readback_vba(root),timeout_seconds=timeout())
                mesh_report = model.read_mesh_report(case, root, model_report)
                _write(root/'mesh-readback.json',mesh_report)
                threads = mesh_report.get('mesher_threads')
                if type(threads) is not int or threads != runtime.get('max_cpus'):
                    raise ValueError('actual mesher thread count differs from immutable controller runtime')
            except Exception:
                reason_code = 'native_mesh_validation_failed'
                raise
            state['unresolved_gates'].extend(mesh_report.get('unresolved_gates',[]))
            stage('save_final_project')
            session.save(include_results=True, timeout_seconds=timeout())
            stage('close_owned_project')
            session.close_project(timeout_seconds=timeout())
            adapter,raw_export = export_raw()
            if raw_export.get('status') != 'completed':
                reason_code = 'native_result_export_incomplete'
                raise ValueError('actual result-tree export incomplete; raw data/errors retained')
            if self.result_profile is not None:
                stage('map_explicit_results')
                mapping = adapter.canonicalize_raw_results(raw_export,self.result_profile,case,
                                                           model_report,root/'mapped-results')
                _write(root/'result-mapping.json',mapping)
                if mapping.get('status') != 'diagnostic_only' or not mapping.get('spectra_path') or not mapping.get('power_path'):
                    reason_code = 'native_result_mapping_failed'
                    raise ValueError('actual result mapping failed; mapping evidence retained')
                state['unresolved_gates'].extend(mapping.get('unresolved_gates',[]))
                stage('analyze_and_plot')
                from .metrics import analyze_exports
                from .plotting import plot_rl
                analysis = analyze_exports(case,Path(mapping['spectra_path']),Path(mapping['power_path']))
                injected = state['backend_evidence'] != 'native_sdk'
                analysis.update(status='diagnostic_only',quality_state='diagnostic_only',
                                numerically_qualified=False,native_acceptance='not_run',
                                solver_evidence=state['backend_evidence'],
                                CST_execution='not_run_injected_test_interface' if injected else state['CST_execution'])
                analysis['provenance'].update(export_origin='injected_fixture_project' if injected else 'owned_saved_native_project',
                                               transmission_source=mapping.get('transmission_source'),
                                               transmission_exported=False,
                                               mapping_evidence='profile_declared_not_native_accepted')
                _write(root/'analysis/metrics.json',analysis)
                plot_rl(analysis,root/'analysis',case)
            else:
                state['unresolved_gates'].append('explicit_actual_result_mapping')
            checkpoint()
            # Accepted native readbacks are distinct from a completed solver call.
            if not self.acceptance_run and not state['unresolved_gates'] and mapping is not None:
                status,reason_code = 'completed','mapped_execution_complete_unqualified'
            else:
                status,reason_code = 'failed','native_validation_pending'
        except BaseException as error:
            state['errors'].append({'type':type(error).__name__,'message':str(error),'stage':state['stage']})
            from .native_sdk import SessionCancelled
            cancellation = (isinstance(error,SessionCancelled)
                            or isinstance(error,NativeRunStopped) and error.caller_stop)
            state['interruption_acknowledged'] = cancellation and event.is_set()
            if isinstance(error,NativeRunStopped) or state['interruption_acknowledged']:
                reason_code = 'native_run_interrupted'
            elif reason_code == 'native_validation_pending':
                reason_code = 'native_stage_failed'
            # A stopped solver can still have useful failed/partial native data.
            # Preserve it within the remaining budget without changing failure.
            if (session is not None and solver_finished and raw_export is None
                    and not event.is_set() and deadline is not None and time.monotonic()<deadline):
                try:
                    stage('preserve_failed_solver_project')
                    session.save(include_results=True,timeout_seconds=timeout())
                    session.close_project(timeout_seconds=timeout())
                    _,raw_export = export_raw()
                except BaseException as preserve_error:
                    state['errors'].append({'type':type(preserve_error).__name__,'message':str(preserve_error),
                                            'stage':'preserve_failed_solver_results'})
            if session is not None and solver_start_requested and not solver_finished:
                try:
                    session.abort_solver(timeout_seconds=self.cleanup_timeout_seconds)
                    state['stage_log'].append('abort_owned_solver')
                except BaseException as abort_error:
                    state['errors'].append({'type':type(abort_error).__name__,'message':str(abort_error),'stage':'abort_owned_solver'})
        finally:
            if session is not None:
                self._close_attempted = True
                try:
                    closed = session.close(timeout_seconds=self.cleanup_timeout_seconds)
                    owned_closed = isinstance(closed,dict) and closed.get('owned_closed') is True
                    state['closure'] = closed
                except BaseException as close_error:
                    owned_closed = False
                    state['errors'].append({'type':type(close_error).__name__,'message':str(close_error),'stage':'close_owned_session'})
            elif creation_requested:
                owned_closed = False
            if not owned_closed:
                status,reason_code = 'failed','owned_closure_unconfirmed'
            if state['backend_evidence'] == 'injected_test_interface':
                state['CST_execution'] = 'not_run_injected_test_interface'
            state['unresolved_gates'] = sorted(set(state['unresolved_gates']))
            state.update(status=status,reason_code=reason_code,owned_closed=owned_closed,
                         retryable=False,validation='not_run')
            _write(root/'native-run.json',state)
            artifacts=[]
            try:
                for path in root.rglob('*'):
                    relative=path.relative_to(root)
                    if relative.parts[0]=='temp' or relative.as_posix() in ('inputs.json','runtime.json','receipt.json'):
                        continue
                    if path.is_symlink():
                        raise ValueError('native output symlink is not an owned artifact')
                    if path.is_file(): artifacts.append(relative.as_posix())
                if len(artifacts)>100000:
                    raise ValueError('native artifact count exceeds controller bound')
            except Exception as error:
                state.update(status='failed',reason_code='native_artifact_manifest_failed')
                state['errors'].append({'type':type(error).__name__,'message':str(error),'stage':'artifact_manifest'})
                _write(root/'native-run.json',state)
            self.last_receipt = {**state,'artifacts':sorted(artifacts),'backend':'cst',
                                 'reason':state['reason_code'], 'physical_certification':False}
        return copy.deepcopy(self.last_receipt)
