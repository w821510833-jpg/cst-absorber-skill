"""Offline safety tests: no solver launch or real process operations."""
import copy
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
try:
    from cst_absorber import runtime
except ImportError:
    runtime = None


def plan(ids=("design",), **limits):
    return {"schema_version": "1.0", "scope_mode": "single" if len(ids) == 1 else "batch",
            "cases": [{"id": name, "signature": name, "geometry": {"volume": 1}} for name in ids],
            "runtime": {"max_cpus": 1, "min_available_RAM_GiB": 0,
                        "min_free_disk_GiB": 0, "max_modes": 10,
                        "wall_budget_seconds": 2, **limits}, "content_hash": "untrusted"}


def resources(root):
    assert Path(root).is_dir()
    return {"available_RAM_GiB": 8, "free_disk_GiB": 8}


@contextmanager
def stalled_runtime_read(attribute, filenames, enabled=lambda: True):
    """Track and drain only synthetic/injected I/O, even if a probe never starts."""
    from unittest.mock import patch
    original = getattr(runtime, attribute)
    original_async = runtime._async_call
    release = threading.Event()
    entered = threading.Event()
    threads = []
    def stalled(path, *args, **kwargs):
        if Path(path).name in filenames and enabled():
            entered.set()
            release.wait(1)
        return original(path, *args, **kwargs)
    def traced_async(function, *args):
        thread, returned = original_async(function, *args)
        threads.append(thread)
        return thread, returned
    with patch.object(runtime, attribute, side_effect=stalled), patch.object(runtime, "_async_call", side_effect=traced_async):
        try:
            yield entered
        finally:
            release.set()
            # Joining parents before their children also catches newly appended
            # nested reads. Keep patches and source fixtures alive until joined.
            for thread in threads:
                thread.join(2)
            if any(thread.is_alive() for thread in threads):
                raise AssertionError("injected read threads must finish before fixture cleanup")


class Worker:
    def __init__(self):
        self.calls = []

    def __call__(self, case, root, stop):
        self.calls.append((case["id"], root))
        (root / "result.txt").write_text(case["id"], encoding="utf-8")
        return {"status": "completed", "owned_closed": True, "artifacts": ["result.txt"]}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(runtime, "runtime safety implementation must exist")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "run"

    def run_plan(self, configuration=None, worker=None, **kwargs):
        return runtime.run_cases(configuration or plan(), self.root, worker or Worker(),
                                 resource_probe=resources, **kwargs)

    def test_single_runs_exactly_once_and_resume_does_not_repeat(self):
        worker = Worker()
        result = self.run_plan(worker=worker)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(worker.calls), 1)
        self.assertFalse(result["physical_certification"])
        again = self.run_plan(worker=worker, resume=True)
        self.assertEqual(again["status"], "completed")
        self.assertEqual(len(worker.calls), 1)
        self.assertTrue((worker.calls[0][1] / "inputs.json").is_file())

    def test_pause_requested_by_resource_probe_prevents_dispatch(self):
        worker = Worker()
        def probe(root):
            runtime.request_pause(root)
            return resources(root)
        result = runtime.run_cases(plan(), self.root, worker, probe)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(worker.calls, [])
        self.assertEqual(result["cases"][0]["attempts"], [])
        self.assertEqual(self.run_plan(worker=worker, resume=True)["status"], "completed")
        self.assertEqual(len(worker.calls), 1)

    def test_hot_pause_reads_share_wall_deadline(self):
        reads = 0
        worker = Worker()
        def third():
            nonlocal reads
            reads += 1
            return reads == 3
        with stalled_runtime_read("_pause_snapshot", {"pause.json"}, third) as entered:
            started = time.monotonic()
            result = self.run_plan(plan(wall_budget_seconds=.15), worker)
            self.assertTrue(entered.is_set())
            self.assertLess(time.monotonic() - started, .5)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(worker.calls, [])

    def test_active_pause_read_shares_deadline_and_signals_owned_worker(self):
        started_worker = threading.Event()
        stopped = threading.Event()
        def worker(case, directory, stop):
            started_worker.set()
            stop.wait(1)
            if stop.is_set():
                stopped.set()
            return {"status": "failed", "owned_closed": True, "artifacts": []}
        with stalled_runtime_read("_pause_snapshot", {"pause.json"}, started_worker.is_set) as entered:
            started = time.monotonic()
            result = self.run_plan(plan(wall_budget_seconds=.15), worker)
            self.assertTrue(entered.is_set())
            self.assertLess(time.monotonic() - started, .6)
            self.assertEqual(result["status"], "blocked")
            self.assertTrue(stopped.is_set())

    def test_pause_setter_rejects_dangling_link_and_nonboolean_marker(self):
        from unittest.mock import patch
        self.root.mkdir()
        marker = self.root / "pause.json"
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda path: path == marker or original(path)):
            with self.assertRaises(runtime.RuntimeSafetyError):
                runtime.request_pause(self.root)
        self.assertFalse(marker.exists())
        marker.write_text('{"schema_version":"1.0","pause":1}', encoding="utf-8")
        with self.assertRaises(runtime.RuntimeSafetyError):
            runtime.request_pause(self.root)
        self.assertEqual(json.loads(marker.read_text(encoding="utf-8"))["pause"], 1)

    def test_pause_after_final_asset_check_records_unstarted_attempt_and_resumes(self):
        from unittest.mock import patch
        worker = Worker()
        original = runtime._check_assets
        checks = []
        def paused_check(case):
            checks.append(case)
            original(case)
            if len(checks) == 2:
                runtime.request_pause(self.root)
        with patch.object(runtime, "_check_assets", side_effect=paused_check):
            result = self.run_plan(worker=worker)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(worker.calls, [])
        attempt = result["cases"][0]["attempts"][0]
        self.assertIs(attempt["worker_dispatched"], False)
        receipt = json.loads((self.root / attempt["directory"] / "receipt.json").read_text(encoding="utf-8"))
        self.assertIs(receipt["controller_not_started"], True)
        self.assertNotIn("worker_status", receipt)
        self.assertNotIn("interruption_acknowledged", receipt)
        self.assertEqual(self.run_plan(worker=worker, resume=True)["status"], "completed")
        self.assertEqual(worker.calls[0][1].name, "attempt-0002")

    def test_batch_executes_only_explicit_ordered_list(self):
        worker = Worker()
        result = self.run_plan(plan(("a", "b")), worker)
        self.assertEqual(result["status"], "completed")
        self.assertEqual([name for name, _ in worker.calls], ["a", "b"])
        invalid = plan(("a", "b"))
        invalid["scope_mode"] = "single"
        with self.assertRaises(ValueError):
            runtime.run_cases(invalid, self.root / "invalid", worker, resources)

    def test_pause_stops_before_first_worker_and_corrupt_pause_blocks(self):
        runtime.request_pause(self.root)
        worker = Worker()
        self.assertEqual(self.run_plan(worker=worker)["status"], "paused")
        self.assertEqual(worker.calls, [])
        (self.root / "pause.json").write_text("broken", encoding="utf-8")
        self.assertEqual(self.run_plan(worker=worker)["status"], "blocked")
        self.assertEqual(worker.calls, [])

    def test_explicit_resume_consumes_initial_pause_and_executes_once(self):
        runtime.request_pause(self.root)
        worker = Worker()
        self.assertEqual(self.run_plan(worker=worker)['status'], 'paused')
        self.assertEqual(worker.calls, [])
        self.assertEqual(self.run_plan(worker=worker, resume=True)['status'], 'completed')
        self.assertFalse((self.root / 'pause.json').exists())
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual(self.run_plan(worker=worker, resume=True)['status'], 'completed')
        self.assertEqual(len(worker.calls), 1)

    def test_resume_paused_batch_skips_already_completed_case(self):
        worker = Worker()
        def pause_after_first(case, directory, stop):
            receipt = worker(case, directory, stop)
            if case['id'] == 'a':
                runtime.request_pause(self.root)
            return receipt
        configuration = plan(('a', 'b'))
        self.assertEqual(self.run_plan(configuration, pause_after_first)['status'], 'paused')
        self.assertEqual([name for name, _ in worker.calls], ['a'])
        self.assertEqual(self.run_plan(configuration, worker, resume=True)['status'], 'completed')
        self.assertEqual([name for name, _ in worker.calls], ['a', 'b'])
        self.assertFalse((self.root / 'pause.json').exists())

    def test_resume_rejects_changed_inputs_or_live_lock_without_consuming_pause(self):
        for variant in ('inputs', 'lock', 'closure', 'output'):
            with self.subTest(variant=variant):
                root = self.root / variant
                worker = Worker()
                configuration = plan()
                if variant == 'inputs':
                    runtime.request_pause(root)
                    self.assertEqual(runtime.run_cases(configuration, root, worker, resources)['status'], 'paused')
                    configuration['cases'][0]['geometry']['volume'] = 2
                else:
                    self.assertEqual(runtime.run_cases(configuration, root, worker, resources)['status'], 'completed')
                    runtime.request_pause(root)
                    if variant == 'lock':
                        (root / 'controller.lock').write_text('{}', encoding='utf-8')
                    elif variant == 'closure':
                        state = json.loads((root / 'state.json').read_text(encoding='utf-8'))
                        state['cases'][0]['attempts'][0]['owned_closed'] = False
                        (root / 'state.json').write_text(json.dumps(state), encoding='utf-8')
                    else:
                        (worker.calls[0][1] / 'result.txt').write_text('changed', encoding='utf-8')
                before = len(worker.calls)
                result = runtime.run_cases(configuration, root, worker, resources, resume=True)
                self.assertEqual(result['status'], 'blocked')
                self.assertTrue((root / 'pause.json').exists())
                self.assertEqual(len(worker.calls), before)

    def test_pause_during_worker_resumes_without_spending_failure_attempt_budget(self):
        worker = Worker()
        def paused_worker(case, directory, stop):
            runtime.request_pause(self.root)
            self.assertTrue(stop.wait(1))
            return {'status': 'failed', 'owned_closed': True, 'artifacts': [],
                    'retryable': False, 'reason': 'native_run_interrupted',
                    'interruption_acknowledged': True}
        configuration = plan(max_attempts=1)
        first = self.run_plan(configuration, paused_worker)
        self.assertEqual(first['status'], 'paused')
        self.assertEqual(first['cases'][0]['status'], 'paused')
        self.assertEqual(self.run_plan(configuration, worker, resume=True)['status'], 'completed')
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual(worker.calls[0][1].name, 'attempt-0002')
        self.assertEqual(len(json.loads((self.root / 'state.json').read_text())['cases'][0]['attempts']), 2)

    def test_pause_does_not_exempt_a_concurrent_nonretryable_failure(self):
        calls = []
        def failed_worker(case, directory, stop):
            calls.append(directory)
            runtime.request_pause(self.root)
            self.assertTrue(stop.wait(1))
            return {'status': 'failed', 'owned_closed': True, 'artifacts': [],
                    'retryable': False, 'reason': 'native_validation_pending'}
        configuration = plan(max_attempts=1)
        first = self.run_plan(configuration, failed_worker)
        self.assertEqual(first['status'], 'paused')
        self.assertEqual(first['cases'][0]['status'], 'failed')
        self.assertEqual(self.run_plan(configuration, failed_worker, resume=True)['status'], 'failed')
        self.assertEqual(len(calls), 1)

    def test_completed_receipt_during_pause_is_not_repeated_on_resume(self):
        worker = Worker()
        def complete_during_pause(case, directory, stop):
            runtime.request_pause(self.root)
            self.assertTrue(stop.wait(1))
            return worker(case, directory, stop)
        first = self.run_plan(worker=complete_during_pause)
        self.assertEqual(first['status'], 'paused')
        self.assertEqual(first['cases'][0]['status'], 'completed')
        self.assertEqual(self.run_plan(worker=worker, resume=True)['status'], 'completed')
        self.assertEqual(len(worker.calls), 1)

    def test_new_pause_during_resume_validation_is_preserved(self):
        from unittest.mock import patch
        worker = Worker()
        self.assertEqual(self.run_plan(worker=worker)['status'], 'completed')
        runtime.request_pause(self.root)
        original = runtime._load_cache
        def renew_pause(*args):
            state = original(*args)
            runtime.request_pause(self.root)
            return state
        with patch.object(runtime, '_load_cache', side_effect=renew_pause):
            self.assertEqual(self.run_plan(worker=worker, resume=True)['status'], 'paused')
        self.assertTrue((self.root / 'pause.json').exists())
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual(self.run_plan(worker=worker, resume=True)['status'], 'completed')
        self.assertFalse((self.root / 'pause.json').exists())

    def test_changed_inputs_cannot_reuse_cache_even_with_same_claimed_hash(self):
        worker = Worker()
        self.run_plan(worker=worker)
        changed = plan()
        changed["cases"][0]["geometry"]["volume"] = 2
        self.assertEqual(self.run_plan(changed, worker, resume=True)["status"], "blocked")
        self.assertEqual(len(worker.calls), 1)

    def test_output_and_inputs_tampering_block_resume(self):
        worker = Worker()
        self.run_plan(worker=worker)
        artifact = worker.calls[0][1] / "result.txt"
        artifact.write_text("tampered", encoding="utf-8")
        self.assertEqual(self.run_plan(worker=worker, resume=True)["status"], "blocked")
        self.assertEqual(len(worker.calls), 1)

    def test_input_snapshot_tampering_blocks_resume(self):
        worker = Worker()
        self.run_plan(worker=worker)
        (worker.calls[0][1] / "inputs.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.run_plan(worker=worker, resume=True)["status"], "blocked")

    def test_worker_reads_separate_runtime_limits_snapshot(self):
        seen = []
        def worker(case, directory, stop):
            seen.append(json.loads((directory / "runtime.json").read_text(encoding="utf-8")))
            self.assertNotIn("runtime", case)
            return {"status": "completed", "owned_closed": True, "artifacts": []}
        result = self.run_plan(plan(max_cpus=3, max_modes=12), worker)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(seen[0]["max_cpus"], 3)
        self.assertEqual(seen[0]["max_modes"], 12)

    def test_runtime_snapshot_tampering_blocks_resume(self):
        worker = Worker()
        self.run_plan(worker=worker)
        (worker.calls[0][1] / "runtime.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.run_plan(worker=worker, resume=True)["status"], "blocked")
        self.assertEqual(len(worker.calls), 1)

    def test_worker_runtime_snapshot_mutation_blocks_completion(self):
        def worker(case, directory, stop):
            (directory / "runtime.json").write_text("{}", encoding="utf-8")
            return {"status": "completed", "owned_closed": True, "artifacts": []}
        self.assertEqual(self.run_plan(worker=worker)["status"], "blocked")

    def test_runtime_changes_do_not_change_completed_physical_identity(self):
        worker = Worker()
        self.run_plan(worker=worker)
        changed_limits = plan(max_cpus=2, wall_budget_seconds=3)
        self.assertEqual(self.run_plan(changed_limits, worker, resume=True)["status"], "completed")
        self.assertEqual(len(worker.calls), 1)

    def test_relative_artifact_cannot_escape_owned_attempt(self):
        def worker(case, root, stop):
            return {"status": "completed", "owned_closed": True, "artifacts": ["../../outside.txt"]}
        self.assertEqual(self.run_plan(worker=worker)["status"], "blocked")

    def test_resource_failure_never_starts_worker(self):
        worker = Worker()
        def broken(root):
            raise OSError("probe unavailable")
        result = runtime.run_cases(plan(), self.root, worker, broken)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(worker.calls, [])

    def test_resource_shortage_never_starts_worker(self):
        worker = Worker()
        result = runtime.run_cases(plan(min_available_RAM_GiB=20), self.root, worker, resources)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(worker.calls, [])

    def test_resource_monitor_stops_running_worker(self):
        calls = []
        stopped = threading.Event()
        def probe(root):
            calls.append(root)
            return {"available_RAM_GiB": 8 if len(calls) == 1 else 0, "free_disk_GiB": 8}
        def worker(case, root, stop):
            stop.wait(1)
            if stop.is_set():
                stopped.set()
            return {"status": "failed", "owned_closed": True, "artifacts": []}
        result = runtime.run_cases(plan(min_available_RAM_GiB=1), self.root, worker, probe)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(stopped.is_set())

    def test_wall_budget_signals_stop_and_unclosed_receipt_blocks_recovery(self):
        stopped = threading.Event()
        def worker(case, root, stop):
            stop.wait(1)
            stopped.set()
            return {"status": "failed", "owned_closed": False, "artifacts": []}
        started = time.monotonic()
        result = self.run_plan(plan(wall_budget_seconds=.2), worker)
        self.assertLess(time.monotonic() - started, .8)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(stopped.is_set())
        replacement = Worker()
        self.assertEqual(self.run_plan(plan(wall_budget_seconds=.2), replacement, resume=True)["status"], "blocked")
        self.assertEqual(replacement.calls, [])

    def test_noncooperative_worker_is_bounded_and_keeps_lock(self):
        release = threading.Event()
        self.addCleanup(release.set)
        def worker(case, root, stop):
            release.wait(2)
            return {"status": "completed", "owned_closed": True, "artifacts": []}
        started = time.monotonic()
        result = self.run_plan(plan(wall_budget_seconds=.2), worker)
        self.assertLess(time.monotonic() - started, .8)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue((self.root / "controller.lock").exists())

    def test_hanging_resource_probe_is_bounded(self):
        release = threading.Event()
        self.addCleanup(release.set)
        def probe(root):
            release.wait(2)
            return resources(root)
        worker = Worker()
        started = time.monotonic()
        result = runtime.run_cases(plan(wall_budget_seconds=.03), self.root, worker, probe)
        self.assertLess(time.monotonic() - started, .8)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(worker.calls, [])

    def test_completed_requires_explicit_owned_closure(self):
        def worker(case, root, stop):
            return {"status": "completed", "artifacts": []}
        self.assertEqual(self.run_plan(worker=worker)["status"], "blocked")

    def test_closed_failure_retries_only_authorized_number_with_distinct_attempts(self):
        calls = []
        def worker(case, root, stop):
            calls.append(root)
            return {"status": "failed", "owned_closed": True, "artifacts": []}
        self.assertEqual(self.run_plan(plan(max_attempts=2), worker)["status"], "failed")
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0], calls[1])
        self.assertTrue(all((root / "inputs.json").is_file() for root in calls))

    def test_unclosed_failure_never_retries(self):
        calls = []
        def worker(case, root, stop):
            calls.append(root)
            return {"status": "failed", "owned_closed": False, "artifacts": []}
        self.assertEqual(self.run_plan(plan(max_attempts=2), worker)["status"], "blocked")
        self.assertEqual(len(calls), 1)

    def test_nonretryable_failed_receipt_stops_batch_and_resume_without_repeat(self):
        calls = []
        def worker(case, root, stop):
            calls.append(case["id"])
            return {"status": "failed", "owned_closed": True, "artifacts": [],
                    "retryable": False, "reason": "native_validation_pending"}
        configuration = plan(("a", "b"), max_attempts=3)
        result = self.run_plan(configuration, worker)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(calls, ["a"])
        self.assertIn("native_validation_pending", result["reason"])
        configuration["runtime"]["max_attempts"] = 5
        resumed = self.run_plan(configuration, worker, resume=True)
        self.assertEqual(resumed["status"], "failed")
        self.assertEqual(calls, ["a"])

    def test_invalid_retryable_receipt_values_fail_closed_without_retry(self):
        for value in (0, 1, "false", None):
            with self.subTest(value=value):
                calls = []
                def worker(case, root, stop):
                    calls.append(root)
                    return {"status": "failed", "owned_closed": True, "artifacts": [], "retryable": value}
                result = runtime.run_cases(plan(max_attempts=3), self.root / str(value), worker, resources)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(len(calls), 1)

    def test_foreign_pid_reuse_and_outside_path_never_cleanup(self):
        self.root.mkdir()
        session = {"session_id": "owned", "pid": 123, "created_at": "epoch1",
                   "executable": str(self.root / "solver.exe"), "run_dir": str(self.root)}
        self.assertTrue(runtime.verify_owned(session, copy.deepcopy(session), self.root))
        calls = []
        for key, value in (("pid", 999), ("created_at", "epoch2"),
                           ("session_id", "foreign"), ("executable", str(self.root.parent / "foreign.exe")),
                           ("run_dir", str(self.root.parent))):
            live = {**session, key: value}
            self.assertFalse(runtime.verify_owned(session, live, self.root))
            self.assertFalse(runtime.safe_cleanup(session, live, self.root, lambda identity: calls.append(identity)))
        self.assertEqual(calls, [])

    def test_verified_cleanup_calls_only_injected_identity_action(self):
        self.root.mkdir()
        identity = {"session_id": "owned", "pid": 123, "created_at": "epoch1",
                    "executable": str(self.root / "solver.exe"), "run_dir": str(self.root)}
        calls = []
        def action(item):
            calls.append(item)
            return {"owned_closed": True}
        self.assertTrue(runtime.safe_cleanup(identity, identity.copy(), self.root, action))
        self.assertEqual(calls, [identity])

    def test_unsorted_artifacts_resume_without_repeating(self):
        calls = []
        def worker(case, root, stop):
            calls.append(root)
            for name in ("z.txt", "a.txt"):
                (root / name).write_text(name, encoding="utf-8")
            return {"status": "completed", "owned_closed": True, "artifacts": ["z.txt", "a.txt"]}
        self.assertEqual(self.run_plan(worker=worker)["status"], "completed")
        self.assertEqual(self.run_plan(worker=worker, resume=True)["status"], "completed")
        self.assertEqual(len(calls), 1)

    def test_status_is_read_only_and_reports_corrupt_state(self):
        self.assertEqual(runtime.read_status(self.root)["status"], "not_started")
        self.assertFalse(self.root.exists())
        self.run_plan()
        before = (self.root / "state.json").read_bytes()
        self.assertEqual(runtime.read_status(self.root)["status"], "completed")
        self.assertEqual((self.root / "state.json").read_bytes(), before)
        (self.root / "state.json").write_text("{bad", encoding="utf-8")
        self.assertEqual(runtime.read_status(self.root)["status"], "blocked")

    def test_status_reports_pending_pause_and_uncertain_lock(self):
        runtime.request_pause(self.root)
        self.assertEqual(runtime.read_status(self.root)["status"], "paused")
        (self.root / "pause.json").unlink()
        (self.root / "controller.lock").write_text("{}", encoding="utf-8")
        self.assertEqual(runtime.read_status(self.root)["status"], "blocked")

    def test_cleanup_hook_rejects_foreign_identity_on_timeout(self):
        release = threading.Event()
        self.addCleanup(release.set)
        class OwnedWorker:
            def __init__(self):
                self.cleaned = []
            def __call__(self, case, directory, stop):
                release.wait(2)
                return {"status": "failed", "owned_closed": False, "artifacts": []}
            def ownership(self, directory):
                identity = {"session_id": "ours", "pid": 123, "created_at": "epoch1",
                            "executable": str(directory / "solver.exe"), "run_dir": str(directory)}
                return identity, {**identity, "created_at": "reused"}
            def cleanup_identity(self, identity):
                self.cleaned.append(identity)
                return {"owned_closed": True}
        worker = OwnedWorker()
        self.assertEqual(self.run_plan(plan(wall_budget_seconds=.2), worker)["status"], "blocked")
        self.assertEqual(worker.cleaned, [])

    def test_timeout_cleanup_is_invoked_once_even_if_worker_does_not_exit(self):
        release = threading.Event()
        self.addCleanup(release.set)
        class OwnedWorker:
            def __init__(self):
                self.cleaned = []
            def __call__(self, case, directory, stop):
                release.wait(2)
                return {"status": "failed", "owned_closed": True, "artifacts": []}
            def ownership(self, directory):
                identity = {"session_id": "ours", "pid": 123, "created_at": "epoch1",
                            "executable": str(directory / "solver.exe"), "run_dir": str(directory)}
                return identity, identity.copy()
            def cleanup_identity(self, identity):
                self.cleaned.append(identity)
                return {"owned_closed": True}
        worker = OwnedWorker()
        self.assertEqual(self.run_plan(plan(wall_budget_seconds=.2), worker)["status"], "blocked")
        self.assertEqual(len(worker.cleaned), 1)

    def test_completed_status_detects_output_tampering(self):
        worker = Worker()
        self.run_plan(worker=worker)
        (worker.calls[0][1] / "result.txt").write_text("tampered", encoding="utf-8")
        self.assertEqual(runtime.read_status(self.root)["status"], "blocked")

    def test_interrupt_signals_stop_and_attempt_cleanup(self):
        worker_started = threading.Event()
        stop_seen = threading.Event()
        def worker(case, directory, stop):
            worker_started.set()
            stop.wait(1)
            if stop.is_set():
                stop_seen.set()
            return {"status": "failed", "owned_closed": True, "artifacts": []}
        from unittest.mock import patch
        original = runtime._paused
        def interrupted(root):
            if worker_started.is_set():
                raise KeyboardInterrupt("controller interrupted")
            return original(root)
        with patch.object(runtime, "_paused", side_effect=interrupted):
            self.assertEqual(self.run_plan(worker=worker)["status"], "blocked")
        self.assertTrue(stop_seen.is_set())

    def test_deleting_input_snapshot_blocks_run(self):
        def worker(case, directory, stop):
            (directory / "inputs.json").unlink()
            return {"status": "completed", "owned_closed": True, "artifacts": []}
        self.assertEqual(self.run_plan(worker=worker)["status"], "blocked")

    def test_corrupted_pause_during_worker_stops_and_blocks(self):
        stopped = threading.Event()
        def worker(case, directory, stop):
            (self.root / "pause.json").write_text("corrupt", encoding="utf-8")
            stop.wait(1)
            stopped.set()
            return {"status": "failed", "owned_closed": True, "artifacts": []}
        result = self.run_plan(worker=worker)
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(stopped.is_set())

    def test_corrupt_receipt_blocks_resume(self):
        worker = Worker()
        self.run_plan(worker=worker)
        (worker.calls[0][1] / "receipt.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.run_plan(worker=worker, resume=True)["status"], "blocked")

    def test_one_item_confirmed_batch_is_direct_single_path(self):
        configuration = plan()
        configuration["scope_mode"] = "batch"
        worker = Worker()
        self.assertEqual(self.run_plan(configuration, worker)["status"], "completed")
        self.assertEqual(len(worker.calls), 1)

    def test_invalid_runtime_counts_are_rejected_before_worker(self):
        for key, value in (("max_attempts", 1.5), ("wall_budget_seconds", float("nan")),
                           ("max_cpus", True), ("min_free_disk_GiB", -1)):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    self.run_plan(plan(**{key: value}))

    def stl_plan(self, ids=("design",)):
        configuration = plan(ids)
        assets = []
        for case in configuration["cases"]:
            asset = Path(self.temp.name) / (case["id"] + ".stl")
            asset.write_bytes(b"offline synthetic STL source bytes " + case["id"].encode())
            case["geometry"] = {"regions": [{"id": "body", "kind": "stl", "path": str(asset),
                                              "material": "solid", "source_unit": "m"}]}
            case["geometry_audit"] = {"asset_hashes": {"body": hashlib.sha256(asset.read_bytes()).hexdigest()}}
            assets.append(asset)
        return configuration, assets

    def test_changed_stl_source_blocks_old_plan_resume_without_repeat(self):
        configuration, assets = self.stl_plan()
        worker = Worker()
        self.assertEqual(self.run_plan(configuration, worker)["status"], "completed")
        assets[0].write_bytes(b"changed source bytes")
        self.assertEqual(self.run_plan(configuration, worker, resume=True)["status"], "blocked")
        self.assertEqual(len(worker.calls), 1)

    def test_changed_stl_source_blocks_completed_status(self):
        configuration, assets = self.stl_plan()
        self.assertEqual(self.run_plan(configuration)["status"], "completed")
        assets[0].write_bytes(b"changed source bytes")
        self.assertEqual(runtime.read_status(self.root)["status"], "blocked")

    def test_deleted_stl_source_blocks_resume_and_completed_status(self):
        configuration, assets = self.stl_plan()
        worker = Worker()
        self.assertEqual(self.run_plan(configuration, worker)["status"], "completed")
        assets[0].unlink()
        self.assertEqual(runtime.read_status(self.root)["status"], "blocked")
        self.assertEqual(self.run_plan(configuration, worker, resume=True)["status"], "blocked")
        self.assertEqual(len(worker.calls), 1)

    def test_tampered_snapshot_cannot_redirect_status_into_unaudited_asset(self):
        configuration, assets = self.stl_plan()
        worker = Worker()
        self.assertEqual(self.run_plan(configuration, worker)["status"], "completed")
        foreign_fixture = Path(self.temp.name) / "unaudited.bin"
        foreign_fixture.write_bytes(assets[0].read_bytes())
        snapshot = worker.calls[0][1] / "inputs.json"
        changed = json.loads(snapshot.read_text(encoding="utf-8"))
        changed["geometry"]["regions"][0]["path"] = str(foreign_fixture)
        snapshot.write_text(json.dumps(changed), encoding="utf-8")
        from unittest.mock import patch
        observed = []
        original_hash = runtime._file_hash
        def audited_hash(path):
            observed.append(path)
            return original_hash(path)
        with patch.object(runtime, "_file_hash", side_effect=audited_hash):
            self.assertEqual(runtime.read_status(self.root)["status"], "blocked")
        self.assertNotIn(foreign_fixture, observed)

    def test_missing_stl_source_blocks_initial_run_and_resume(self):
        configuration, assets = self.stl_plan()
        worker = Worker()
        assets[0].unlink()
        self.assertEqual(self.run_plan(configuration, worker)["status"], "blocked")
        self.assertEqual(worker.calls, [])

    def test_missing_stl_audit_or_hash_blocks_before_worker(self):
        configuration, _ = self.stl_plan()
        for variant in ("audit", "hashes", "entry", "bad_hash"):
            with self.subTest(variant=variant):
                changed = copy.deepcopy(configuration)
                case = changed["cases"][0]
                if variant == "audit":
                    case.pop("geometry_audit")
                elif variant == "hashes":
                    case["geometry_audit"].pop("asset_hashes")
                elif variant == "entry":
                    case["geometry_audit"]["asset_hashes"] = {}
                else:
                    case["geometry_audit"]["asset_hashes"] = {"body": "untrusted"}
                worker = Worker()
                result = runtime.run_cases(changed, self.root / variant, worker, resources)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(worker.calls, [])

    def test_relative_stl_source_path_blocks_before_worker(self):
        configuration, _ = self.stl_plan()
        case = configuration["cases"][0]
        case["geometry"]["regions"][0]["path"] = "relative.stl"
        case["geometry_audit"]["asset_hashes"] = {"body": "0" * 64}
        worker = Worker()
        self.assertEqual(self.run_plan(configuration, worker)["status"], "blocked")
        self.assertEqual(worker.calls, [])

    def test_stl_change_between_cases_blocks_second_worker(self):
        configuration, assets = self.stl_plan(("a", "b"))
        calls = []
        def worker(case, directory, stop):
            calls.append(case["id"])
            assets[1].write_bytes(b"changed while first case runs")
            return {"status": "completed", "owned_closed": True, "artifacts": []}
        self.assertEqual(self.run_plan(configuration, worker)["status"], "blocked")
        self.assertEqual(calls, ["a"])

    def test_stl_change_during_resource_probe_blocks_worker_start(self):
        configuration, assets = self.stl_plan()
        worker = Worker()
        def probe(root):
            assets[0].write_bytes(b"changed after initial asset validation")
            return resources(root)
        result = runtime.run_cases(configuration, self.root, worker, probe)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(worker.calls, [])

    def test_hanging_stl_hash_is_bounded_by_wall_budget_before_worker(self):
        configuration, assets = self.stl_plan()
        configuration["runtime"]["wall_budget_seconds"] = .08
        worker = Worker()
        started = time.monotonic()
        with stalled_runtime_read("_file_hash", {assets[0].name}):
            result = self.run_plan(configuration, worker)
            self.assertLess(time.monotonic() - started, .6)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(worker.calls, [])

    def test_hanging_stl_status_probe_has_explicit_bounded_wait(self):
        configuration, assets = self.stl_plan()
        self.assertEqual(self.run_plan(configuration)["status"], "completed")
        started = time.monotonic()
        with stalled_runtime_read("_file_hash", {assets[0].name}):
            result = runtime.read_status(self.root, probe_budget_seconds=.08)
            self.assertLess(time.monotonic() - started, .6)
            self.assertEqual(result["status"], "blocked")
            self.assertIn("timed out", result["reason"])

    def test_preworker_snapshot_hashing_is_bounded_without_starting_worker(self):
        for filename in ("inputs.json", "runtime.json"):
            with self.subTest(filename=filename):
                worker = Worker()
                root = self.root / filename
                started = time.monotonic()
                with stalled_runtime_read("_file_hash", {filename}) as entered:
                    result = runtime.run_cases(plan(wall_budget_seconds=.4), root, worker, resources)
                    self.assertTrue(entered.is_set())
                    self.assertLess(time.monotonic() - started, .85)
                    self.assertEqual(result["status"], "blocked")
                    self.assertEqual(worker.calls, [])

    def test_postworker_hashing_is_bounded_without_publishing_completed_receipt(self):
        for filename in ("result.txt", "inputs.json", "runtime.json", "receipt.json"):
            with self.subTest(filename=filename):
                worker_finished = threading.Event()
                delegate = Worker()
                def worker(case, directory, stop):
                    receipt = delegate(case, directory, stop)
                    worker_finished.set()
                    return receipt
                root = self.root / filename
                filenames = {filename, "receipt.pending.json"} if filename == "receipt.json" else {filename}
                started = time.monotonic()
                with stalled_runtime_read("_file_hash", filenames, worker_finished.is_set) as entered:
                    result = runtime.run_cases(plan(wall_budget_seconds=.4, max_attempts=3), root, worker, resources)
                    self.assertTrue(entered.is_set())
                    self.assertLess(time.monotonic() - started, .85)
                    self.assertEqual(result["status"], "blocked")
                    self.assertEqual(len(delegate.calls), 1)
                    state = json.loads((root / "state.json").read_text(encoding="utf-8"))
                    self.assertEqual(state["status"], "blocked")
                    self.assertNotEqual(state["cases"][0]["status"], "completed")
                    self.assertFalse((delegate.calls[0][1] / "receipt.json").exists())
                    self.assertFalse((root / "controller.lock").exists())
                    blocked_state = (root / "state.json").read_bytes()
                self.assertEqual((root / "state.json").read_bytes(), blocked_state)
                self.assertFalse((delegate.calls[0][1] / "receipt.json").exists())
                self.assertIn("timed out", runtime.read_status(root)["reason"])

    def test_resume_cache_hashing_is_bounded_without_worker_or_cache_upgrade(self):
        for filename in ("result.txt", "inputs.json", "runtime.json", "receipt.json"):
            with self.subTest(filename=filename):
                root = self.root / filename
                initial = runtime.run_cases(plan(), root, Worker(), resources)
                self.assertEqual(initial["status"], "completed")
                before = (root / "state.json").read_bytes()
                replacement = Worker()
                started = time.monotonic()
                with stalled_runtime_read("_file_hash", {filename}) as entered:
                    result = runtime.run_cases(plan(wall_budget_seconds=.4), root, replacement, resources, resume=True)
                    self.assertTrue(entered.is_set())
                    self.assertLess(time.monotonic() - started, .85)
                    self.assertEqual(result["status"], "blocked")
                    self.assertEqual(replacement.calls, [])
                    self.assertEqual((root / "state.json").read_bytes(), before)
                from unittest.mock import patch
                with patch.object(runtime, "_file_hash", side_effect=AssertionError("blocked cache must not be rehashed")) as forbidden_hash:
                    repeated = runtime.run_cases(plan(), root, replacement, resources, resume=True)
                    self.assertEqual(repeated["status"], "blocked")
                    self.assertEqual(runtime.read_status(root)["status"], "blocked")
                    forbidden_hash.assert_not_called()
                self.assertEqual(replacement.calls, [])
                marker = json.loads((root / "blocked.json").read_text(encoding="utf-8"))
                self.assertEqual(marker["status"], "blocked")
                self.assertIn("timed out", marker["reason"])

    def test_resume_cache_metadata_read_is_bounded_without_worker(self):
        self.assertEqual(self.run_plan()["status"], "completed")
        before = (self.root / "state.json").read_bytes()
        worker = Worker()
        started = time.monotonic()
        with stalled_runtime_read("_read", {"state.json"}) as entered:
            result = self.run_plan(plan(wall_budget_seconds=.4), worker, resume=True)
            self.assertTrue(entered.is_set())
            self.assertLess(time.monotonic() - started, .85)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(worker.calls, [])
            self.assertEqual((self.root / "state.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
