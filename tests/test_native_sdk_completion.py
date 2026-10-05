"""Bounded solver/archive completion uses injected SDK and PID observers only.

Fixture files are ordinary temporary files. These tests establish admission,
operation ordering and caller publication, not native CST writer provenance.
"""
import copy
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from test_native_sdk import Harness, native_sdk


class NativeSdkCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "owned"
        self.root.mkdir()
        self.h = Harness(self.root)
        self.session = native_sdk.CstSdkSession(
            self.root, authorized=True, interface_module=self.h.interface,
            identity_probe=self.h.probe, startup_timeout_seconds=2)
        self.session.save(timeout_seconds=2)
        self.old_pin = self.session._saved_file_identity
        self.old_digest = self.session._saved_file_digest
        self.final_payload = b"owned injected final archive with results"
        self.after_run = lambda: None
        self.after_save = lambda: None
        model = self.h.project.model3d
        def run_solver(timeout=None):
            self.h.calls.append(("run_solver", timeout))
            model.running = False
            self.replace_archive(b"injected solver and post-processing archive")
            self.after_run()
        model.run_solver = run_solver
        original_save = self.h.project.save
        def save(*args, **kwargs):
            original_save(*args, **kwargs)
            self.replace_archive(self.final_payload)
            self.after_save()
        self.h.project.save = save

    def run_completion(self, seconds=2):
        method = getattr(self.session, "run_solver_and_snapshot", None)
        self.assertTrue(callable(method), "bounded public solver/archive completion entrypoint is missing")
        return method(timeout_seconds=seconds)

    def replace_archive(self, payload):
        replacement = self.root / "replacement.tmp"
        replacement.write_bytes(payload)
        os.replace(replacement, self.session.project_path)

    def assert_rejected_pin(self):
        self.assertEqual(self.session._saved_file_identity, self.old_pin)
        self.assertEqual(self.session._saved_file_digest, self.old_digest)
        self.assertEqual(self.session.archive_integrity()["state"], "quarantined")

    def assert_no_completion_save(self):
        self.assertEqual([call[0] for call in self.h.calls].count("save"), 1)

    def test_success_dispatches_one_blocking_solver_and_checked_result_save(self):
        result = self.run_completion()
        names = [call[0] for call in self.h.calls]
        self.assertEqual(names.count("run_solver"), 1)
        self.assertNotIn("start", names)
        self.assertEqual(names.count("save"), 2)
        self.assertLess(names.index("run_solver"), names.index("solver_info"))
        self.assertLess(names.index("solver_info"), len(names) - 1)
        self.assertEqual(self.h.calls[-1], ("save", str(self.session.project_path), True, True))
        self.assertEqual(result["completion_method"], "Model3D.run_solver")
        self.assertIsInstance(result["operation_id"], str)
        self.assertTrue(result["operation_id"])
        self.assertEqual(result["solver_info"], self.h.solver_info)
        self.assertEqual(result["archive_integrity"]["writer_attribution"],
                         "explicit_sdk_operation_trust_boundary")
        self.assertEqual(result["archive_integrity"]["pin_operation"], "run_solver_and_snapshot")
        self.assertEqual(result["archive_integrity"]["sha256"], hashlib.sha256(self.final_payload).hexdigest())
        self.assertEqual(self.session.verify_archive(2)["state"], "pinned")

    def test_trace_records_replacement_between_operation_boundaries(self):
        result = self.run_completion()
        events = result["write_trace"]
        self.assertTrue(events)
        self.assertTrue(all(event["operation_id"] == result["operation_id"] for event in events))
        before = next(event for event in events if event["stage"] == "run_solver" and event["phase"] == "before")
        after = next(event for event in events if event["stage"] == "run_solver" and event["phase"] == "after")
        self.assertEqual(before["observed_identity"], list(self.old_pin))
        self.assertNotEqual(before["observed_identity"], after["observed_identity"])
        self.assertEqual(events[-1]["stage"], "accepted")

    def test_cached_trace_is_a_deep_copy_without_filesystem_or_sdk_calls(self):
        self.run_completion()
        reader = getattr(self.session, "archive_write_trace", None)
        self.assertTrue(callable(reader), "cached archive-write observations are missing")
        with mock.patch.object(self.session, "_file_identity", side_effect=AssertionError("filesystem query")):
            with mock.patch.object(self.session, "_guard", side_effect=AssertionError("SDK/PID query")):
                trace = reader()
        trace[0]["stage"] = "forged"
        self.assertNotEqual(reader()[0]["stage"], "forged")

    def test_external_replacement_before_dispatch_is_rejected_without_solver_or_save(self):
        self.replace_archive(b"external pre-operation replacement")
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.run_completion()
        self.assertNotIn("run_solver", [call[0] for call in self.h.calls])
        self.assert_no_completion_save()
        self.assert_rejected_pin()

    def test_same_inode_external_bytes_before_dispatch_are_rejected(self):
        self.session.project_path.write_bytes(b"external pre-operation content")
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.run_completion()
        self.assertNotIn("run_solver", [call[0] for call in self.h.calls])
        self.assert_no_completion_save()
        self.assert_rejected_pin()

    def test_last_predispatch_observer_content_change_prevents_solver_call(self):
        original_snapshot = self.session._archive_snapshot
        original_probe = self.session._probe
        snapshots, changed = [], []
        def snapshot(*args, **kwargs):
            result = original_snapshot(*args, **kwargs)
            snapshots.append(True)
            return result
        def probe(pid):
            if len(snapshots) >= 2 and not changed:
                changed.append(True)
                self.session.project_path.write_bytes(b"external bytes from final predispatch observer")
            return original_probe(pid)
        with mock.patch.object(self.session, "_archive_snapshot", snapshot):
            with mock.patch.object(self.session, "_probe", probe):
                with self.assertRaises(native_sdk.SessionSafetyError):
                    self.run_completion()
        self.assertTrue(changed)
        self.assertNotIn("run_solver", [call[0] for call in self.h.calls])
        self.assert_no_completion_save()
        self.assert_rejected_pin()

    def test_existing_quarantine_never_becomes_a_new_writer_permission(self):
        self.session._quarantine_archive("preexisting sticky reason")
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.run_completion()
        self.assertEqual(self.session.archive_integrity()["reason"], "preexisting sticky reason")
        self.assertNotIn("run_solver", [call[0] for call in self.h.calls])
        self.assert_no_completion_save()

    def test_wrong_project_filename_after_solver_blocks_following_sdk_calls(self):
        self.after_run = lambda: setattr(self.h.project, "saved_filename", str(self.root.parent / "foreign.cst"))
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.run_completion()
        self.assertEqual([call[0] for call in self.h.calls].count("poll"), 1)
        self.assert_no_completion_save()
        self.assert_rejected_pin()

    def test_last_pid_observer_cannot_redirect_filename_at_solver_dispatch(self):
        original_verify = self.session._verify_archive_body
        original_filename = self.h.project.filename
        original_probe = self.session._probe
        original_run = self.h.project.model3d.run_solver
        checks = {"verified": 0, "armed": False, "changed": False}
        dispatched = []
        def verify(*args, **kwargs):
            result = original_verify(*args, **kwargs)
            checks["verified"] += 1
            return result
        def filename():
            result = original_filename()
            if checks["verified"] == 2 and not checks["changed"]:
                checks["armed"] = True
            return result
        def probe(pid):
            result = original_probe(pid)
            if checks["armed"] and not checks["changed"]:
                checks["changed"] = True
                self.h.project.saved_filename = str(self.root.parent / "foreign.cst")
            return result
        def run(timeout=None):
            dispatched.append(original_filename())
            self.h.project.saved_filename = str(self.session.project_path)
            return original_run(timeout)
        with mock.patch.object(self.session, "_verify_archive_body", verify):
            with mock.patch.object(self.session, "_probe", probe):
                with mock.patch.object(self.h.project, "filename", filename):
                    with mock.patch.object(self.h.project.model3d, "run_solver", run):
                        with self.assertRaises(native_sdk.SessionSafetyError):
                            self.run_completion()
        self.assertTrue(checks["changed"])
        self.assertEqual(dispatched, [])
        self.assert_no_completion_save()
        self.assert_rejected_pin()

    def test_swapped_project_after_solver_is_never_used(self):
        foreign = Harness(self.root.parent / "foreign")
        foreign_project = type(self.h.project)(foreign, self.h.de)
        self.after_run = lambda: setattr(self.session, "_project", foreign_project)
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.run_completion()
        self.assertEqual(foreign.calls, [])
        self.assert_no_completion_save()
        self.assert_rejected_pin()

    def test_swapped_environment_after_solver_blocks_result_save(self):
        self.after_run = lambda: setattr(self.session, "_de", object())
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.run_completion()
        self.assert_no_completion_save()
        self.assert_rejected_pin()

    def test_unknown_pid_after_solver_blocks_result_save(self):
        self.after_run = lambda: setattr(self.h, "observation", {"state": "unknown", "pid": 811})
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.run_completion()
        self.assert_no_completion_save()
        self.assert_rejected_pin()

    def test_reused_pid_after_solver_blocks_result_save(self):
        self.after_run = lambda: self.h.observation.update(created_at="filetime:other-process")
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.run_completion()
        self.assert_no_completion_save()
        self.assert_rejected_pin()

    def test_solver_still_running_after_blocking_return_is_rejected(self):
        self.after_run = lambda: setattr(self.h.project.model3d, "running", True)
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.run_completion()
        self.assert_no_completion_save()
        self.assert_rejected_pin()

    def test_preexisting_async_solver_activity_prevents_another_dispatch(self):
        self.h.project.model3d.running = True
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.run_completion()
        self.assertNotIn("run_solver", [call[0] for call in self.h.calls])
        self.assert_no_completion_save()
        self.assert_rejected_pin()

    def test_non_success_info_never_grants_result_save(self):
        self.h.solver_info = {"state": "FAILED", "message": "actual fixture failure"}
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.run_completion()
        self.assert_no_completion_save()
        self.assert_rejected_pin()

    def test_stopped_failed_run_retains_actual_completion_observation(self):
        self.h.solver_info = {"state": "FAILED", "message": "actual fixture diagnostic", "detail": [7]}
        with self.assertRaises(native_sdk.SessionSafetyError) as raised:
            self.run_completion()
        observation = getattr(raised.exception, "completed_observation", None)
        self.assertIsInstance(observation, dict, "actual stopped FAILED observation is missing")
        self.assertEqual(observation["completion_method"], "Model3D.run_solver")
        self.assertIs(observation["solver_running"], False)
        self.assertEqual(observation["solver_info"], self.h.solver_info)
        self.assertTrue(observation["operation_id"])
        self.assertTrue(observation["write_trace"])
        self.h.solver_info["detail"].append(8)
        self.assertEqual(observation["solver_info"]["detail"], [7])
        self.assert_no_completion_save()
        self.assert_rejected_pin()

    def test_stopped_aborted_run_retains_actual_completion_observation(self):
        self.h.solver_info = {"state": "ABORTED", "message": "actual abort diagnostic"}
        with self.assertRaises(native_sdk.SessionSafetyError) as raised:
            self.run_completion()
        observation = getattr(raised.exception, "completed_observation", None)
        self.assertIsInstance(observation, dict, "actual stopped ABORTED observation is missing")
        self.assertIs(observation["solver_running"], False)
        self.assertEqual(observation["solver_info"], self.h.solver_info)
        self.assert_no_completion_save()
        self.assert_rejected_pin()

    def test_preflight_hash_timeout_preserves_timeout_type(self):
        with mock.patch.object(self.session, "_archive_snapshot", side_effect=native_sdk.SessionTimeout("injected hash deadline")):
            with self.assertRaises(native_sdk.SessionTimeout):
                self.run_completion()
        self.assert_rejected_pin()

    def test_non_boolean_running_query_is_rejected(self):
        self.h.project.model3d.is_solver_running = lambda timeout=None: 0
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.run_completion()
        self.assert_no_completion_save()
        self.assert_rejected_pin()

    def test_non_dict_info_is_rejected(self):
        self.h.solver_info = ["SUCCESS"]
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.run_completion()
        self.assert_no_completion_save()
        self.assert_rejected_pin()

    def test_solver_exception_is_not_retried_or_fallen_back_to_start(self):
        def fail(timeout=None):
            self.h.calls.append(("run_solver", timeout))
            self.replace_archive(b"archive from failed solver")
            raise RuntimeError("injected solver failure")
        self.h.project.model3d.run_solver = fail
        with self.assertRaisesRegex(RuntimeError, "injected solver failure"):
            self.run_completion()
        self.assertEqual([call[0] for call in self.h.calls].count("run_solver"), 1)
        self.assertNotIn("start", [call[0] for call in self.h.calls])
        self.assert_no_completion_save()
        self.assert_rejected_pin()

    def test_solver_exception_retains_after_operation_archive_observation(self):
        def fail(timeout=None):
            self.replace_archive(b"changed archive before raised solver return")
            raise RuntimeError("injected solver failure with changed bytes")
        self.h.project.model3d.run_solver = fail
        with self.assertRaisesRegex(RuntimeError, "solver failure with changed bytes"):
            self.run_completion()
        after = [event for event in self.session.archive_write_trace()
                 if event["stage"] == "run_solver" and event["phase"] == "after"]
        self.assertTrue(after, "failed SDK operation must retain its post-call archive observation")
        self.assertNotEqual(after[0]["observed_identity"], list(self.old_pin))

    def test_delayed_caller_rolls_back_to_its_admission_pin(self):
        delayed, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original_request = self.session._request
        outcomes = []
        def request(*args, **kwargs):
            if threading.current_thread().name == "delayed-completion-caller":
                delayed.set()
                release.wait(2)
            return original_request(*args, **kwargs)
        def delayed_caller():
            try:
                self.run_completion()
            except BaseException as error:
                outcomes.append(error)
        with mock.patch.object(self.session, "_request", request):
            caller = threading.Thread(target=delayed_caller, name="delayed-completion-caller")
            caller.start()
            try:
                self.assertTrue(delayed.wait(2))
                first = self.run_completion()
                first_pin = self.session._saved_file_identity
                first_digest = self.session._saved_file_digest
                self.assertNotEqual(first_pin, self.old_pin)
                def fail(timeout=None):
                    raise RuntimeError("second admitted run failed")
                self.h.project.model3d.run_solver = fail
                release.set()
                caller.join(2)
                self.assertFalse(caller.is_alive())
            finally:
                release.set()
                caller.join(2)
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], RuntimeError)
        self.assertEqual(self.session._saved_file_identity, first_pin)
        self.assertEqual(self.session._saved_file_digest, first_digest)
        self.assertEqual(first["archive_integrity"]["sha256"], first_digest)
        self.assertEqual(self.session.archive_integrity()["state"], "quarantined")

    def test_delayed_ordinary_save_cannot_restore_precompletion_pin(self):
        delayed, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original_request = self.session._request
        outcomes = []
        def request(*args, **kwargs):
            if threading.current_thread().name == "delayed-ordinary-save":
                delayed.set()
                release.wait(2)
            return original_request(*args, **kwargs)
        def delayed_save():
            try:
                self.session.save(timeout_seconds=2)
            except BaseException as error:
                outcomes.append(error)
        with mock.patch.object(self.session, "_request", request):
            caller = threading.Thread(target=delayed_save, name="delayed-ordinary-save")
            caller.start()
            try:
                self.assertTrue(delayed.wait(2))
                self.run_completion()
                first_pin = self.session._saved_file_identity
                first_digest = self.session._saved_file_digest
                self.session._stop_event.set()
                release.set()
                caller.join(2)
                self.assertFalse(caller.is_alive())
            finally:
                release.set()
                caller.join(2)
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], native_sdk.SessionCancelled)
        self.assertEqual(self.session._saved_file_identity, first_pin)
        self.assertEqual(self.session._saved_file_digest, first_digest)
        self.assertEqual(self.session.archive_integrity()["state"], "quarantined")

    def test_completion_save_failure_cannot_commit_solver_archive(self):
        def fail(*args, **kwargs):
            self.h.calls.append(("failed_save",))
            self.replace_archive(b"archive from failed final save")
            raise RuntimeError("injected completion save failure")
        self.h.project.save = fail
        with self.assertRaisesRegex(RuntimeError, "completion save failure"):
            self.run_completion()
        self.assert_rejected_pin()

    def test_hardlinked_solver_archive_is_rejected_before_save(self):
        foreign = self.root.parent / "foreign.cst"
        foreign.write_bytes(b"foreign hardlink target")
        def link_after_run():
            self.session.project_path.unlink()
            os.link(foreign, self.session.project_path)
        self.after_run = link_after_run
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.run_completion()
        self.assert_no_completion_save()
        self.assertEqual(foreign.read_bytes(), b"foreign hardlink target")
        self.assert_rejected_pin()

    def test_nonregular_solver_archive_is_rejected_before_save(self):
        def directory_after_run():
            self.session.project_path.unlink()
            self.session.project_path.mkdir()
        self.after_run = directory_after_run
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.run_completion()
        self.assert_no_completion_save()
        self.assert_rejected_pin()

    def test_same_inode_bytes_changing_between_complete_hashes_are_rejected(self):
        original = self.session._archive_snapshot
        changed = []
        def snapshot(*args, **kwargs):
            result = original(*args, **kwargs)
            if [call[0] for call in self.h.calls].count("save") == 2 and not changed:
                changed.append(True)
                self.session.project_path.write_bytes(b"changed between full stable snapshots")
            return result
        with mock.patch.object(self.session, "_archive_snapshot", snapshot):
            with self.assertRaises(native_sdk.SessionSafetyError):
                self.run_completion()
        self.assertTrue(changed)
        self.assert_rejected_pin()

    def test_post_snapshot_observer_change_is_detected_before_publication(self):
        original_snapshot = self.session._archive_snapshot
        original_probe = self.session._probe
        hashed = []
        changed = []
        def snapshot(*args, **kwargs):
            result = original_snapshot(*args, **kwargs)
            if [call[0] for call in self.h.calls].count("save") == 2:
                hashed.append(True)
            return result
        def probe(pid):
            if hashed and not changed:
                changed.append(True)
                self.session.project_path.write_bytes(b"changed by final ownership observation")
            return original_probe(pid)
        with mock.patch.object(self.session, "_archive_snapshot", snapshot):
            with mock.patch.object(self.session, "_probe", probe):
                with self.assertRaises(native_sdk.SessionSafetyError):
                    self.run_completion()
        self.assertTrue(changed)
        self.assert_rejected_pin()

    def reject_final_snapshot_closure_flag(self, flag):
        original_snapshot = self.session._archive_snapshot
        final_hashes = []
        def snapshot(*args, **kwargs):
            result = original_snapshot(*args, **kwargs)
            if [call[0] for call in self.h.calls].count("save") == 2:
                final_hashes.append(True)
                if len(final_hashes) == 3:
                    setattr(self.session, flag, True)
            return result
        with mock.patch.object(self.session, "_archive_snapshot", snapshot):
            with self.assertRaises(native_sdk.SessionSafetyError):
                self.run_completion()
        self.assertEqual(len(final_hashes), 3)
        self.assert_rejected_pin()

    def test_final_snapshot_cannot_publish_after_owned_close_flag_changes(self):
        self.reject_final_snapshot_closure_flag("_owned_closed")

    def test_final_snapshot_cannot_publish_while_environment_close_is_unconfirmed(self):
        self.reject_final_snapshot_closure_flag("_de_close_returned")

    def test_stop_after_solver_return_prevents_save_and_pin(self):
        self.after_run = self.session._stop_event.set
        with self.assertRaises(native_sdk.SessionCancelled):
            self.run_completion()
        self.assert_no_completion_save()
        self.assert_rejected_pin()

    def test_revoked_internal_token_never_reopens_after_solver_return(self):
        self.after_run = lambda: setattr(self.session, "_archive_writer_token", None)
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.run_completion()
        self.assert_no_completion_save()
        self.assert_rejected_pin()

    def test_timeout_keeps_pending_native_lifetime_and_late_solver_cannot_save(self):
        gate, entered = threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        original = self.h.project.model3d.run_solver
        def delayed(timeout=None):
            entered.set()
            gate.wait(2)
            return original(timeout)
        self.h.project.model3d.run_solver = delayed
        with self.assertRaises(native_sdk.SessionTimeout):
            self.run_completion(.15)
        self.assertTrue(entered.is_set())
        self.assertTrue(self.session.supervision_status()["request_pending"])
        count = len(self.h.calls)
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.session.abort_solver(.05)
        self.assertEqual(len(self.h.calls), count)
        gate.set()
        self.session._active_thread.join(2)
        self.assertFalse(self.session._active_thread.is_alive())
        self.assert_no_completion_save()
        self.assert_rejected_pin()
        self.assertTrue(self.session.close(2)["owned_closed"])

    def test_late_preflight_hash_cannot_mark_quarantined_archive_verified(self):
        release, hashed = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original_snapshot = self.session._archive_snapshot
        def snapshot(*args, **kwargs):
            result = original_snapshot(*args, **kwargs)
            if not hashed.is_set():
                hashed.set()
                release.wait(2)
            return result
        with mock.patch.object(self.session, "_archive_snapshot", snapshot):
            with self.assertRaises(native_sdk.SessionTimeout):
                self.run_completion(.15)
            self.assertTrue(hashed.is_set())
            release.set()
            self.session._active_thread.join(2)
        self.assert_rejected_pin()
        self.assertIs(self.session.archive_integrity()["archive_integrity_verified"], False)
        self.assertNotIn("run_solver", [call[0] for call in self.h.calls])

    def test_timeout_after_writing_state_handoff_prevents_deferred_solver_dispatch(self):
        lock = self.session._state_lock
        paused, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        paused_once = []
        class PausedUnlock:
            def __enter__(inner):
                lock.acquire()
                return inner
            def __exit__(inner, *args):
                pause = (threading.current_thread().name == "owned-cst-solver-completion"
                         and self.session._archive["state"] == "writing" and not paused_once)
                if pause:
                    paused_once.append(True)
                lock.release()
                if pause:
                    paused.set()
                    release.wait(2)
        try:
            with mock.patch.object(self.session, "_state_lock", PausedUnlock()):
                with self.assertRaises(native_sdk.SessionTimeout):
                    self.run_completion(.15)
                self.assertTrue(paused.is_set())
                release.set()
                self.session._active_thread.join(2)
            self.assertNotIn("run_solver", [call[0] for call in self.h.calls])
            self.assert_no_completion_save()
            self.assert_rejected_pin()
        finally:
            release.set()
            self.session._active_thread.join(2)

    def test_on_accept_publishes_memory_only(self):
        original_request = self.session._request
        checked = []
        def request(label, function, seconds, **kwargs):
            accept = kwargs["on_accept"]
            def memory_only(prepared, deadline):
                checked.append(True)
                with mock.patch.object(self.session, "_archive_snapshot", side_effect=AssertionError("hash during accept")):
                    with mock.patch.object(self.session, "_guard", side_effect=AssertionError("PID/SDK during accept")):
                        with mock.patch.object(self.session, "_check_path", side_effect=AssertionError("path during accept")):
                            with mock.patch.object(self.session, "_file_identity", side_effect=AssertionError("stat during accept")):
                                return accept(prepared, deadline)
            kwargs["on_accept"] = memory_only
            return original_request(label, function, seconds, **kwargs)
        with mock.patch.object(self.session, "_request", request):
            self.run_completion()
        self.assertTrue(checked)

    def test_prepared_candidate_late_completion_publication_cannot_commit(self):
        self.assertTrue(callable(getattr(self.session, "run_solver_and_snapshot", None)),
                        "bounded public solver/archive completion entrypoint is missing")
        release, entered = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original_event = threading.Event
        events = [0]
        class BlockedCompletion(original_event):
            def set(self):
                entered.set()
                release.wait(2)
                return super().set()
            def wait(self, timeout=None):
                if not entered.wait(2):
                    raise AssertionError("completion worker did not reach publication")
                return False
        def event_factory(*args, **kwargs):
            events[0] += 1
            return BlockedCompletion(*args, **kwargs) if events[0] == 3 else original_event(*args, **kwargs)
        try:
            with mock.patch.object(threading, "Event", side_effect=event_factory):
                with mock.patch.object(native_sdk.time, "monotonic", return_value=0):
                    with self.assertRaises(native_sdk.SessionTimeout):
                        self.run_completion(.15)
                    self.assertTrue(entered.is_set())
                    self.assert_rejected_pin()
                    release.set()
                    self.session._active_thread.join(2)
            self.assertFalse(self.session.supervision_status()["request_pending"])
            self.assert_rejected_pin()
        finally:
            release.set()
            self.session._active_thread.join(2)

    def test_replacement_after_completion_is_rejected_by_existing_content_gate(self):
        self.run_completion()
        accepted_pin = self.session._saved_file_identity
        self.replace_archive(b"external replacement after completed write window")
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.session.save(timeout_seconds=2)
        self.assertEqual(self.session._saved_file_identity, accepted_pin)
        self.assertEqual(self.session.archive_integrity()["state"], "quarantined")


if __name__ == "__main__":
    unittest.main()
