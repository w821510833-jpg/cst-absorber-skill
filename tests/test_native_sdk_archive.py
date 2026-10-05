"""Archive changes cannot strand an exactly owned injected SDK session.

Every interface/process observation is a Python fixture. No vendor imports or
real process calls occur. The production transport, filesystem and timeouts are
real: an external replacement must never become permission to save/export.
"""
import os
from pathlib import Path
import tempfile
import threading
from unittest import mock
import unittest

from test_native_sdk import Harness, native_sdk


class NativeSdkArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "owned"
        self.root.mkdir()
        self.h = Harness(self.root)
        self.session = native_sdk.CstSdkSession(
            self.root, authorized=True, interface_module=self.h.interface,
            identity_probe=self.h.probe, startup_timeout_seconds=.5)
        self.session.save(timeout_seconds=.5)

    def replace_archive(self, payload=b"unattributed replacement archive"):
        replacement = self.root / "replacement.tmp"
        replacement.write_bytes(payload)
        os.replace(replacement, self.session.project_path)
        return payload

    def interrupted_bootstrap(self, name):
        """Launch a real Python thread, interrupt before its _started handshake.

        SDK/process interfaces remain injected. Thread.start really did launch
        the callback's OS thread, so is_alive(False) cannot mean not launched.
        """
        gate = threading.Event()
        launched = []
        original_thread = threading.Thread
        class InterruptedThread(original_thread):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.interrupt_this = self.name == name and not launched
            def _bootstrap_inner(self):
                if self.interrupt_this:
                    gate.wait(2)
                return super()._bootstrap_inner()
            def start(self):
                if not self.interrupt_this:
                    return super().start()
                launched.append(self)
                original_wait = self._started.wait
                def interrupt(*args, **kwargs):
                    raise KeyboardInterrupt("injected interruption after Python thread launch")
                self._started.wait = interrupt
                try:
                    return super().start()
                finally:
                    self._started.wait = original_wait
        self.addCleanup(gate.set)
        return InterruptedThread, gate, launched

    def assert_content_actions_blocked(self):
        count = len(self.h.calls)
        for method, args in (("save", ()), ("add_to_history", ("new", "code")),
                             ("execute_vba", ("code",)), ("start_solver", ()),
                             ("verify_archive", ())):
            with self.subTest(method=method):
                with self.assertRaises(native_sdk.SessionSafetyError):
                    getattr(self.session, method)(*args, timeout_seconds=.5)
                self.assertEqual(len(self.h.calls), count)

    def assert_owned_controls_and_close(self, payload):
        self.assertIsInstance(self.session.is_solver_running(.5), bool)
        self.assertEqual(self.session.get_solver_run_info(.5), self.h.solver_info)
        self.session.abort_solver(.5)
        self.assertFalse(self.session.is_solver_running(.5))
        self.assertTrue(self.session.close(.5)["owned_closed"])
        self.assertEqual(self.session.project_path.read_bytes(), payload)
        self.assertEqual([c[0] for c in self.h.calls].count("save"), 1)
        self.assertEqual([c[0] for c in self.h.calls].count("project_close"), 1)
        self.assertEqual([c[0] for c in self.h.calls].count("de_close"), 1)

    def test_start_atomic_archive_replace_keeps_controls_and_discard_close(self):
        original = self.h.project.model3d.start_solver
        payload = b"injected solver archive after atomic rewrite"
        def start(timeout=None):
            original(timeout=timeout)
            self.replace_archive(payload)
        self.h.project.model3d.start_solver = start
        self.session.start_solver(.5)
        self.assert_owned_controls_and_close(payload)

    def test_async_archive_replace_after_start_keeps_controls_and_close(self):
        self.session.start_solver(.5)
        ready, replaced = threading.Event(), threading.Event()
        payload = b"injected delayed solver archive"
        def late_write():
            ready.wait(1)
            self.replace_archive(payload)
            replaced.set()
        writer = threading.Thread(target=late_write)
        writer.start()
        self.addCleanup(writer.join, 1)
        ready.set()
        self.assertTrue(replaced.wait(.5))
        self.assert_owned_controls_and_close(payload)

    def test_external_idle_replace_quarantines_content_without_blocking_cleanup(self):
        payload = self.replace_archive()
        self.assert_content_actions_blocked()
        self.assertEqual(self.session.archive_integrity()["state"], "quarantined")
        self.assert_owned_controls_and_close(payload)

    def test_external_inplace_content_edit_is_not_accepted_as_same_inode_snapshot(self):
        self.session.project_path.write_bytes(b"external in-place modification")
        self.assert_content_actions_blocked()
        self.assert_owned_controls_and_close(b"external in-place modification")

    def test_control_observation_records_archive_quarantine_without_repinning(self):
        old_identity = self.session._saved_file_identity
        self.replace_archive()
        self.session.is_solver_running(.5)
        receipt = self.session.archive_integrity()
        self.assertEqual(receipt["state"], "quarantined")
        self.assertEqual(self.session._saved_file_identity, old_identity)
        receipt["state"] = "forged"
        self.assertEqual(self.session.archive_integrity()["state"], "quarantined")

    def test_successful_explicit_atomic_save_establishes_a_new_checked_pin(self):
        original = self.h.project.save
        def atomic_save(path="", include_results=True, allow_overwrite=False):
            original(path, include_results=include_results, allow_overwrite=allow_overwrite)
            self.replace_archive(b"injected explicit SDK atomic save")
        self.h.project.save = atomic_save
        self.session.save(timeout_seconds=.5)
        verified = self.session.verify_archive(timeout_seconds=.5)
        self.assertEqual(verified["state"], "pinned")
        self.assertEqual(verified["pin_operation"], "save")
        self.assertEqual(verified["writer_attribution"], "explicit_sdk_save_trust_boundary")
        self.session.add_to_history("after-save", "code", .5)

    def test_failed_save_cannot_commit_changed_archive(self):
        pin = self.session._saved_file_identity
        def failed_save(*args, **kwargs):
            self.replace_archive(b"changed during failed SDK save")
            raise RuntimeError("injected save failure")
        self.h.project.save = failed_save
        with self.assertRaisesRegex(RuntimeError, "save failure"):
            self.session.save(timeout_seconds=.5)
        self.assertEqual(self.session._saved_file_identity, pin)
        self.assert_content_actions_blocked()
        self.assertTrue(self.session.close(.5)["owned_closed"])

    def test_late_save_completion_after_timeout_cannot_commit_changed_archive(self):
        pin = self.session._saved_file_identity
        gate, entered = threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        def late_save(*args, **kwargs):
            entered.set()
            gate.wait(1)
            self.replace_archive(b"changed after caller timed out")
        self.h.project.save = late_save
        with self.assertRaises(native_sdk.SessionTimeout):
            self.session.save(timeout_seconds=.05)
        self.assertTrue(entered.is_set())
        before = len(self.h.calls)
        self.assertFalse(self.session.close(.05)["owned_closed"])
        self.assertEqual(len(self.h.calls), before)
        gate.set()
        self.session._active_thread.join(.5)
        self.assertFalse(self.session._active_thread.is_alive())
        self.assertEqual(self.session._saved_file_identity, pin)
        self.assert_content_actions_blocked()
        self.assertTrue(self.session.close(.5)["owned_closed"])

    def test_exact_original_already_gone_confirms_close_without_sdk_actions(self):
        self.h.observation = {"state": "gone", "pid": 811}
        count = len(self.h.calls)
        receipt = self.session.close(.5)
        self.assertTrue(receipt["owned_closed"])
        self.assertEqual(len(self.h.calls), count)
        self.assertEqual(receipt["closure_kind"], "original_process_already_gone")

    def test_exact_original_pid_reused_confirms_without_touching_new_process(self):
        self.h.observation["created_at"] = "filetime:reused"
        count = len(self.h.calls)
        self.assertTrue(self.session.close(.5)["owned_closed"])
        self.assertEqual(len(self.h.calls), count)

    def test_wrong_sdk_filename_still_blocks_controls_and_close(self):
        self.replace_archive()
        self.h.project.saved_filename = str(self.root.parent / "foreign.cst")
        count = len(self.h.calls)
        for method in (self.session.is_solver_running, self.session.abort_solver,
                       self.session.get_solver_run_info, self.session.close_project):
            with self.assertRaises(native_sdk.SessionSafetyError):
                method(.5)
        self.assertFalse(self.session.close(.5)["owned_closed"])
        self.assertEqual(len(self.h.calls), count)

    def test_missing_or_hardlinked_archive_does_not_strand_held_project_close(self):
        for mode in ("missing", "hardlinked"):
            with self.subTest(mode=mode):
                if mode == "hardlinked":
                    # Start a fresh owned fixture after the preceding close.
                    self.h = Harness(self.root)
                    self.session = native_sdk.CstSdkSession(
                        self.root, authorized=True, interface_module=self.h.interface,
                        identity_probe=self.h.probe, startup_timeout_seconds=.5)
                    self.session.save(timeout_seconds=.5)
                self.session.project_path.unlink()
                if mode == "hardlinked":
                    foreign = self.root.parent / "foreign.cst"
                    foreign.write_bytes(b"foreign archive must never be overwritten")
                    os.link(foreign, self.session.project_path)
                self.assert_content_actions_blocked()
                self.assertTrue(self.session.close(.5)["owned_closed"])
                if mode == "hardlinked":
                    self.assertEqual(foreign.read_bytes(), b"foreign archive must never be overwritten")

    def test_fixed_mesh_readback_after_replacement_uses_control_plane_only(self):
        payload = self.replace_archive()
        original = self.h.project.schematic.execute_vba_code
        def query(code, /, timeout=None):
            original(code, timeout=timeout)
            (self.root / "native_mesh.tsv").write_text("injected readback, not native evidence", encoding="utf-8")
        self.h.project.schematic.execute_vba_code = query
        self.session.execute_readback(kind="mesh", timeout_seconds=.5)
        code = [c[1] for c in self.h.calls if c[0] == "vba"][-1]
        self.assertIn("Mesh.Get", code)
        self.assertNotIn("Save", code)
        self.assertEqual(self.session.project_path.read_bytes(), payload)
        self.assert_content_actions_blocked()
        self.assertTrue(self.session.close(.5)["owned_closed"])

    def test_readback_cannot_accept_arbitrary_macro_kind_or_destination(self):
        count = len(self.h.calls)
        for kind in ("reference_plane", "Save", "mesh\nSave", "../mesh", None):
            with self.subTest(kind=kind):
                with self.assertRaises((native_sdk.SessionSafetyError, ValueError)):
                    self.session.execute_readback(kind=kind, timeout_seconds=.5)
        with self.assertRaises(TypeError):
            self.session.execute_readback(kind="mesh", code="Save", timeout_seconds=.5)
        self.assertEqual(len(self.h.calls), count)

    def test_mesh_readback_refuses_preexisting_unowned_destination(self):
        (self.root / "native_mesh.tsv").write_bytes(b"preexisting must not be overwritten")
        count = len(self.h.calls)
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.session.execute_readback(kind="mesh", timeout_seconds=.5)
        self.assertEqual(len(self.h.calls), count)
        self.assertEqual((self.root / "native_mesh.tsv").read_bytes(), b"preexisting must not be overwritten")

    def test_supervision_reports_pending_and_never_concurrently_calls_cleanup(self):
        self.h.history_gate = threading.Event()
        self.addCleanup(self.h.history_gate.set)
        with self.assertRaises(native_sdk.SessionTimeout):
            self.session.add_to_history("slow", "code", timeout_seconds=.05)
        report = self.session.supervision_status()
        self.assertTrue(report["request_pending"])
        self.assertFalse(report["session_creation_pending"])
        self.assertTrue(report["session_created"])
        self.assertFalse(report["owned_closed"])
        count = len(self.h.calls)
        self.assertFalse(self.session.supervise_cleanup(timeout_seconds=.05)["owned_closed"])
        self.assertEqual(len(self.h.calls), count)
        self.h.history_gate.set()
        self.session._active_thread.join(.5)
        self.assertFalse(self.session.supervision_status()["request_pending"])
        self.assertTrue(self.session.supervise_cleanup(timeout_seconds=.5)["owned_closed"])

    def test_held_project_object_replacement_never_authorizes_controls(self):
        self.session._project = type(self.h.project)(self.h, self.h.de)
        self.session._project.saved_filename = str(self.session.project_path)
        count = len(self.h.calls)
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.session.is_solver_running(.5)
        self.assertFalse(self.session.close(.5)["owned_closed"])
        self.assertEqual(len(self.h.calls), count)

    def test_identity_change_during_strict_snapshot_prevents_following_mutation(self):
        original = self.session._archive_snapshot
        def snapshot(*args, **kwargs):
            result = original(*args, **kwargs)
            self.h.observation["created_at"] = "filetime:changed-during-snapshot"
            return result
        count = len(self.h.calls)
        with mock.patch.object(self.session, "_archive_snapshot", snapshot):
            with self.assertRaises(native_sdk.SessionSafetyError):
                self.session.add_to_history("unsafe", "code", .5)
        self.assertEqual(len(self.h.calls), count)

    def test_cancel_during_snapshot_preserves_typed_cancellation(self):
        with mock.patch.object(self.session, "_archive_snapshot",
                               side_effect=native_sdk.SessionCancelled("cancel during hash")):
            with self.assertRaises(native_sdk.SessionCancelled):
                self.session.add_to_history("cancelled", "code", .5)

    def test_failed_save_marks_archive_unverified_even_without_observed_replacement(self):
        def failed_save(*args, **kwargs):
            raise RuntimeError("injected save fails before reporting stable content")
        self.h.project.save = failed_save
        with self.assertRaises(RuntimeError):
            self.session.save(timeout_seconds=.5)
        self.assertFalse(self.session.archive_integrity()["archive_integrity_verified"])
        self.assertEqual(self.session.archive_integrity()["state"], "quarantined")
        self.assertTrue(self.session.close(.5)["owned_closed"])

    def test_unknown_or_foreign_executable_after_quarantine_never_controls(self):
        self.replace_archive()
        for observation in ({"state": "unknown", "pid": 811},
                            {**self.h.observation, "executable": str(self.root / "foreign.exe")}):
            with self.subTest(observation=observation):
                self.h.observation = observation
                count = len(self.h.calls)
                with self.assertRaises(native_sdk.SessionSafetyError):
                    self.session.abort_solver(.5)
                self.assertFalse(self.session.close(.05)["owned_closed"])
                self.assertEqual(len(self.h.calls), count)

    def test_deleted_owned_run_still_blocks_path_bound_close(self):
        self.session.project_path.unlink()
        (self.root / "temp").rmdir()
        self.root.rmdir()
        count = len(self.h.calls)
        self.assertFalse(self.session.close(.1)["owned_closed"])
        self.assertEqual(len(self.h.calls), count)

    def test_late_startup_keeps_pending_unknown_and_then_real_fixture_closure(self):
        other_root = self.root.parent / "late-owned"
        other_root.mkdir()
        harness = Harness(other_root)
        harness.new_gate = threading.Event()
        self.addCleanup(harness.new_gate.set)
        with self.assertRaises(native_sdk.SessionTimeout) as raised:
            native_sdk.CstSdkSession(other_root, authorized=True,
                                     interface_module=harness.interface,
                                     identity_probe=harness.probe, startup_timeout_seconds=.05)
        late = raised.exception.session
        status = late.supervision_status()
        self.assertTrue(status["request_pending"])
        self.assertTrue(status["session_creation_pending"])
        self.assertEqual(status["session_created"], "unknown")
        self.assertFalse(status["owned_closed"])
        count = len(harness.calls)
        self.assertFalse(late.supervise_cleanup(.05)["owned_closed"])
        self.assertEqual(len(harness.calls), count)
        harness.new_gate.set()
        late._active_thread.join(.5)
        self.assertFalse(late.supervision_status()["request_pending"])
        self.assertTrue(late.supervision_status()["owned_closed"])
        self.assertTrue(late.supervise_cleanup(.5)["owned_closed"])
        self.assertNotIn("new_mws", [c[0] for c in harness.calls])

    def test_closed_owned_project_can_strictly_verify_unchanged_export_archive(self):
        self.session.close_project(.5)
        with mock.patch.object(self.h.project, "filename",
                               side_effect=AssertionError("closed project filename must not be queried")):
            report = self.session.verify_archive(.5)
        self.assertTrue(report["archive_integrity_verified"])
        self.assertEqual(report["state"], "pinned")
        self.replace_archive()
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.session.verify_archive(.5)
        self.assertTrue(self.session.close(.5)["owned_closed"])

    def test_project_switch_during_filename_never_calls_foreign_abort(self):
        foreign = Harness(self.root.parent / "foreign-project")
        foreign_project = type(self.h.project)(foreign, self.h.de)
        foreign_project.saved_filename = str(self.session.project_path)
        original = self.h.project.filename
        def filename():
            self.session._project = foreign_project
            return original()
        self.h.project.filename = filename
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.session.abort_solver(.5)
        self.assertEqual(foreign.calls, [])
        self.assertNotIn("abort", [c[0] for c in self.h.calls])

    def test_environment_switch_inside_final_close_guard_never_closes_foreign_de(self):
        self.session.close_project(.5)
        foreign = Harness(self.root.parent / "foreign-environment")
        foreign_de = foreign.interface.DesignEnvironment()
        original_probe = self.session._probe
        observations = [0]
        def probe(pid):
            observations[0] += 1
            # Initial close observation, first guard, final pre-close guard.
            if observations[0] == 3:
                self.session._de = foreign_de
            return original_probe(pid)
        self.session._probe = probe
        self.assertFalse(self.session.close(.1)["owned_closed"])
        self.assertEqual(foreign.calls, [])
        self.assertNotIn("de_close", [c[0] for c in self.h.calls])

    def test_late_final_filename_after_save_timeout_cannot_commit_pin(self):
        old_pin = self.session._saved_file_identity
        old_digest = self.session._saved_file_digest
        original_save = self.h.project.save
        def atomic_save(*args, **kwargs):
            original_save(*args, **kwargs)
            self.replace_archive(b"new explicit save awaiting final filename")
        self.h.project.save = atomic_save
        gate, entered = threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        original_filename = self.h.project.filename
        filename_calls = [0]
        def filename():
            filename_calls[0] += 1
            if filename_calls[0] == 3:
                entered.set()
                gate.wait(1)
            return original_filename()
        self.h.project.filename = filename
        with self.assertRaises(native_sdk.SessionTimeout):
            self.session.save(timeout_seconds=.05)
        self.assertTrue(entered.is_set())
        self.assertTrue(self.session.supervision_status()["request_pending"])
        gate.set()
        self.session._active_thread.join(.5)
        self.assertFalse(self.session._active_thread.is_alive())
        self.assertEqual(self.session._saved_file_identity, old_pin)
        self.assertEqual(self.session._saved_file_digest, old_digest)
        self.assertEqual(self.session.archive_integrity()["state"], "quarantined")
        self.assertFalse(self.session.archive_integrity()["archive_integrity_verified"])

    def test_final_save_filename_object_switch_cannot_commit_pin(self):
        old_pin = self.session._saved_file_identity
        old_digest = self.session._saved_file_digest
        original_save = self.h.project.save
        def atomic_save(*args, **kwargs):
            original_save(*args, **kwargs)
            self.replace_archive(b"new explicit save before held object switch")
        self.h.project.save = atomic_save
        foreign = Harness(self.root.parent / "foreign-project")
        foreign_project = type(self.h.project)(foreign, self.h.de)
        foreign_project.saved_filename = str(self.session.project_path)
        original_filename = self.h.project.filename
        filename_calls = [0]
        def filename():
            filename_calls[0] += 1
            if filename_calls[0] == 3:
                self.session._project = foreign_project
            return original_filename()
        self.h.project.filename = filename
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.session.save(timeout_seconds=.5)
        self.assertEqual(self.session._saved_file_identity, old_pin)
        self.assertEqual(self.session._saved_file_digest, old_digest)
        self.assertEqual(foreign.calls, [])

    def test_closed_archive_snapshot_project_switch_is_not_accepted(self):
        self.session.close_project(.5)
        foreign = Harness(self.root.parent / "foreign-project")
        foreign_project = type(self.h.project)(foreign, self.h.de)
        original_snapshot = self.session._archive_snapshot
        def snapshot(*args, **kwargs):
            result = original_snapshot(*args, **kwargs)
            self.session._project = foreign_project
            return result
        with mock.patch.object(self.session, "_archive_snapshot", snapshot):
            with self.assertRaises(native_sdk.SessionSafetyError):
                self.session.verify_archive(.5)
        self.assertEqual(foreign.calls, [])

    def test_late_final_file_identity_after_save_timeout_cannot_commit_pin(self):
        old_pin = self.session._saved_file_identity
        old_digest = self.session._saved_file_digest
        original_save = self.h.project.save
        def atomic_save(*args, **kwargs):
            original_save(*args, **kwargs)
            self.replace_archive(b"new explicit save awaiting final archive stat")
        self.h.project.save = atomic_save
        original_identity = self.session._file_identity
        gate, entered = threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        stat_calls = [0]
        def identity():
            stat_calls[0] += 1
            if stat_calls[0] == 3:
                entered.set()
                gate.wait(1)
            return original_identity()
        with mock.patch.object(self.session, "_file_identity", identity):
            with self.assertRaises(native_sdk.SessionTimeout):
                self.session.save(timeout_seconds=.05)
            self.assertTrue(entered.is_set())
            gate.set()
            self.session._active_thread.join(.5)
        self.assertEqual(self.session._saved_file_identity, old_pin)
        self.assertEqual(self.session._saved_file_digest, old_digest)
        self.assertEqual(self.session.archive_integrity()["state"], "quarantined")

    def test_interrupted_thread_start_retains_request_until_callback_completion(self):
        fixture, bootstrap, launched = self.interrupted_bootstrap("owned-cst-history")
        self.h.history_gate = threading.Event()
        self.addCleanup(self.h.history_gate.set)
        try:
            with mock.patch.object(threading, "Thread", fixture):
                with self.assertRaises(KeyboardInterrupt):
                    self.session.add_to_history("interrupted-first", "code", .5)
                original = launched[0]
                self.assertFalse(original.is_alive())
                self.assertTrue(self.session.supervision_status()["request_pending"])
                with self.assertRaises(native_sdk.SessionSafetyError):
                    self.session.add_to_history("forbidden-second", "code", .05)
                self.assertIs(self.session._active_thread, original)
                self.assertFalse(self.session.supervise_cleanup(.05)["owned_closed"])
                bootstrap.set()
                self.assertTrue(original._started.wait(.5))
                self.h.history_gate.set()
                original.join(.5)
                self.assertFalse(original.is_alive())
                self.assertFalse(self.session.supervision_status()["request_pending"])
                self.session.add_to_history("allowed-after-completion", "code", .5)
                headers = [c[1] for c in self.h.calls if c[0] == "history"]
                self.assertNotIn("forbidden-second", headers)
                self.assertIn("allowed-after-completion", headers)
        finally:
            bootstrap.set()
            self.h.history_gate.set()
            for thread in launched:
                if thread._started.wait(.5):
                    thread.join(.5)

    def test_interrupted_startup_is_unknown_until_cancelled_callback_proves_no_launch(self):
        root = self.root.parent / "interrupted-startup"
        root.mkdir()
        harness = Harness(root)
        fixture, bootstrap, launched = self.interrupted_bootstrap("owned-cst-startup")
        try:
            with mock.patch.object(threading, "Thread", fixture):
                with self.assertRaises(KeyboardInterrupt) as raised:
                    native_sdk.CstSdkSession(root, authorized=True,
                                             interface_module=harness.interface,
                                             identity_probe=harness.probe, startup_timeout_seconds=.5)
                late = raised.exception.session
                report = late.supervision_status()
                self.assertTrue(report["request_pending"])
                self.assertTrue(report["session_creation_pending"])
                self.assertEqual(report["session_created"], "unknown")
                self.assertFalse(report["owned_closed"])
                self.assertFalse(late.supervise_cleanup(.05)["owned_closed"])
                self.assertEqual(harness.calls, [])
                bootstrap.set()
                self.assertTrue(launched[0]._started.wait(.5))
                launched[0].join(.5)
                report = late.supervision_status()
                self.assertFalse(report["request_pending"])
                self.assertFalse(report["session_creation_pending"])
                self.assertIs(report["session_created"], False)
                self.assertTrue(report["owned_closed"])
                self.assertEqual(harness.calls, [])
                self.assertTrue(late.supervise_cleanup(.5)["owned_closed"])
        finally:
            bootstrap.set()
            for thread in launched:
                if thread._started.wait(.5):
                    thread.join(.5)

    def test_start_exception_without_completion_keeps_indeterminate_reservation(self):
        original_thread = threading.Thread
        class IndeterminateStart(original_thread):
            def start(self):
                raise KeyboardInterrupt("injected start outcome not observable by transport")
        with mock.patch.object(threading, "Thread", IndeterminateStart):
            with self.assertRaises(KeyboardInterrupt):
                self.session.add_to_history("indeterminate", "code", .5)
        report = self.session.supervision_status()
        self.assertTrue(report["request_pending"])
        self.assertFalse(report["owned_closed"])
        count = len(self.h.calls)
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.session.abort_solver(.5)
        self.assertFalse(self.session.supervise_cleanup(.05)["owned_closed"])
        self.assertEqual(len(self.h.calls), count)

    def test_caller_timeout_revokes_save_pin_even_before_clock_deadline_tick(self):
        """Event timeout is authoritative even with a coarse monotonic clock."""
        old_pin = self.session._saved_file_identity
        old_digest = self.session._saved_file_digest
        original_save = self.h.project.save
        def atomic_save(*args, **kwargs):
            original_save(*args, **kwargs)
            self.replace_archive(b"explicit save after unaccepted caller timeout")
        self.h.project.save = atomic_save
        gate, entered = threading.Event(), threading.Event()
        self.addCleanup(gate.set)
        original_filename = self.h.project.filename
        calls = [0]
        def filename():
            calls[0] += 1
            if calls[0] == 3:
                entered.set()
                gate.wait(1)
            return original_filename()
        self.h.project.filename = filename
        with mock.patch.object(native_sdk.time, "monotonic", return_value=0.):
            with self.assertRaises(native_sdk.SessionTimeout):
                self.session.save(timeout_seconds=.05)
            self.assertTrue(entered.is_set())
            gate.set()
            self.session._active_thread.join(.5)
            self.assertFalse(self.session._active_thread.is_alive())
        self.assertEqual(self.session._saved_file_identity, old_pin)
        self.assertEqual(self.session._saved_file_digest, old_digest)
        self.assertEqual(self.session.archive_integrity()["state"], "quarantined")
        self.assertTrue(self.session.close(.5)["owned_closed"])

    def test_save_snapshot_prepared_before_callback_publication_does_not_commit_after_timeout(self):
        old_pin = self.session._saved_file_identity
        old_digest = self.session._saved_file_digest
        original_save = self.h.project.save
        def atomic_save(*args, **kwargs):
            original_save(*args, **kwargs)
            self.replace_archive(b"prepared snapshot awaiting callback completion publication")
        self.h.project.save = atomic_save
        release, entered = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original_event = threading.Event
        events = [0]
        class BlockedCompletion(original_event):
            def set(self):
                entered.set()
                release.wait(1)
                return super().set()
            def wait(self, timeout=None):
                # Produce the caller's false wait only after the worker has
                # prepared its snapshot and reached completion publication.
                # Filesystem/thread scheduling must not decide which safety
                # boundary this regression exercises.
                if not entered.wait(1):
                    raise AssertionError("save callback did not reach completion publication")
                return False
        def event_factory(*args, **kwargs):
            events[0] += 1
            # _request's ready, decision, completed events; Thread follows.
            return BlockedCompletion(*args, **kwargs) if events[0] == 3 else original_event(*args, **kwargs)
        try:
            with mock.patch.object(threading, "Event", side_effect=event_factory):
                with mock.patch.object(native_sdk.time, "monotonic", return_value=0.):
                    with self.assertRaises(native_sdk.SessionTimeout):
                        self.session.save(timeout_seconds=.05)
                    self.assertTrue(entered.is_set())
                    self.assertEqual(self.session._saved_file_identity, old_pin)
                    self.assertEqual(self.session._saved_file_digest, old_digest)
                    release.set()
                    self.session._active_thread.join(.5)
            self.assertFalse(self.session.supervision_status()["request_pending"])
            self.assertEqual(self.session._saved_file_identity, old_pin)
            self.assertEqual(self.session._saved_file_digest, old_digest)
            self.assertEqual(self.session.archive_integrity()["state"], "quarantined")
        finally:
            release.set()
            self.session._active_thread.join(.5)

    def unaccepted_first_save(self):
        root = self.root.parent / "first-unaccepted-save"
        root.mkdir()
        harness = Harness(root)
        session = native_sdk.CstSdkSession(root, authorized=True,
                                           interface_module=harness.interface,
                                           identity_probe=harness.probe, startup_timeout_seconds=.5)
        harness.save_gate = threading.Event()
        self.addCleanup(harness.save_gate.set)
        with self.assertRaises(native_sdk.SessionTimeout):
            session.save(timeout_seconds=.05)
        harness.save_gate.set()
        session._active_thread.join(.5)
        self.assertFalse(session.supervision_status()["request_pending"])
        self.assertFalse(session._saved)
        self.assertIsNone(session._saved_file_identity)
        self.assertEqual(harness.project.filename(), str(session.project_path))
        self.assertEqual(session.archive_integrity()["state"], "quarantined")
        return harness, session

    def test_unaccepted_first_save_discards_exact_saved_project_without_binding_archive(self):
        harness, session = self.unaccepted_first_save()
        payload = session.project_path.read_bytes()
        with self.assertRaises(native_sdk.SessionSafetyError):
            session.verify_archive(.5)
        self.assertTrue(session.close(.5)["owned_closed"])
        self.assertEqual(session.project_path.read_bytes(), payload)
        self.assertEqual([c[0] for c in harness.calls].count("project_close"), 1)
        self.assertEqual([c[0] for c in harness.calls].count("save"), 1)
        self.assertFalse(session._saved)
        self.assertIsNone(session._saved_file_identity)
        self.assertEqual(session.archive_integrity()["state"], "quarantined")

    def test_unaccepted_first_save_foreign_filename_cannot_authorize_discard_close(self):
        harness, session = self.unaccepted_first_save()
        harness.project.saved_filename = str(self.root.parent / "foreign.cst")
        count = len(harness.calls)
        self.assertFalse(session.close(.1)["owned_closed"])
        self.assertEqual(len(harness.calls), count)


if __name__ == "__main__":
    unittest.main()
