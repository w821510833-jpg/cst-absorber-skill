"""SDK transport tests use injected SDK objects and process observations only."""
import copy
import importlib
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
try:
    from cst_absorber import native_sdk
except ImportError:
    native_sdk = None


class FakeModel:
    def __init__(self, harness):
        self.h = harness
        self.running = False

    def add_to_history(self, header, code, /, timeout=None):
        self.h.calls.append(("history", header, code, timeout))
        if self.h.history_gate is not None:
            self.h.history_gate.wait(2)

    def start_solver(self, timeout=None):
        self.h.calls.append(("start", timeout))
        self.running = True

    def is_solver_running(self, timeout=None):
        self.h.calls.append(("poll", timeout))
        return self.running

    def abort_solver(self, timeout=None):
        self.h.calls.append(("abort", timeout))
        if not self.h.abort_stuck:
            self.running = False

    def get_solver_run_info(self, timeout=None):
        self.h.calls.append(("solver_info", timeout))
        return copy.deepcopy(self.h.solver_info)


class FakeSchematic:
    def __init__(self, harness):
        self.h = harness

    def execute_vba_code(self, code, /, timeout=None):
        self.h.calls.append(("vba", code, timeout))


class FakeProject:
    def __init__(self, harness, de):
        self.h = harness
        self.design_environment = de
        self.model3d = FakeModel(harness)
        self.schematic = FakeSchematic(harness)
        self.saved_filename = ""

    def save(self, path="", include_results=True, allow_overwrite=False):
        self.h.calls.append(("save", str(path), include_results, allow_overwrite))
        if self.h.save_gate is not None:
            self.h.save_gate.wait(2)
        target = Path(path)
        if target.exists() and not allow_overwrite:
            raise FileExistsError(str(target))
        target.write_bytes(b"injected project, not CST data")
        self.saved_filename = str(target)

    def filename(self):
        return self.saved_filename

    def close(self):
        self.h.calls.append(("project_close",))
        if self.h.project_close_error:
            raise RuntimeError("injected project close failure")
        if self.h.project_close_gate is not None:
            self.h.project_close_gate.wait(2)


class Harness:
    def __init__(self, root):
        self.calls = []
        self.observation = {"state": "alive", "pid": 811,
                            "created_at": "filetime:123456",
                            "executable": str(root / "fake-cst.exe")}
        self.probed = []
        self.close_mode = "gone"
        self.new_gate = self.project_gate = self.history_gate = None
        self.save_gate = self.project_close_gate = None
        self.de_close_gate = None
        self.new_returned = threading.Event()
        self.de_closed = threading.Event()
        self.observed_gone = threading.Event()
        self.project_close_error = False
        self.abort_stuck = False
        self.solver_info = {"state": "SUCCESS", "undocumented_extra": [1, 2]}
        self.temp_environment = None
        harness = self

        class FakeDE:
            @staticmethod
            def new(*, env=None):
                harness.calls.append(("new",))
                if env is not None:
                    harness.temp_environment = {key: env.get(key) for key in ("TEMP", "TMP", "CST_FAKE_MARKER")}
                if harness.new_gate is not None:
                    harness.new_gate.wait(2)
                de = FakeDE()
                harness.de = de
                harness.new_returned.set()
                return de

            def pid(self):
                harness.calls.append(("pid",))
                return 811

            def new_mws(self):
                harness.calls.append(("new_mws",))
                if harness.project_gate is not None:
                    harness.project_gate.wait(2)
                harness.project = FakeProject(harness, self)
                return harness.project

            def close(self):
                harness.calls.append(("de_close",))
                if harness.de_close_gate is not None:
                    harness.de_close_gate.wait(2)
                if harness.close_mode == "error":
                    raise RuntimeError("injected DE close failure")
                if harness.close_mode == "gone":
                    harness.observation = {"state": "gone", "pid": 811}
                elif harness.close_mode == "reused":
                    harness.observation["created_at"] = "filetime:999999"
                elif harness.close_mode == "unknown":
                    harness.observation = {"state": "unknown", "pid": 811,
                                           "reason": "injected access denied"}
                harness.de_closed.set()

        self.interface = types.SimpleNamespace(DesignEnvironment=FakeDE)

    def probe(self, pid):
        self.probed.append(pid)
        if self.observation.get("state") == "gone":
            self.observed_gone.set()
        return copy.deepcopy(self.observation)


class NativeSdkTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(native_sdk, "documented SDK transport must be implemented")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "owned"
        self.root.mkdir()
        self.h = Harness(self.root)

    def session(self, **kwargs):
        return native_sdk.CstSdkSession(self.root, authorized=True,
                                       interface_module=self.h.interface,
                                       identity_probe=self.h.probe, **kwargs)

    def saved(self):
        session = self.session()
        session.save(timeout_seconds=.3)
        return session

    def test_unauthorized_rejects_before_import_or_new(self):
        with mock.patch.object(importlib, "import_module", side_effect=AssertionError("SDK import")):
            with self.assertRaises(PermissionError):
                native_sdk.CstSdkSession(self.root)
            with self.assertRaises(PermissionError):
                native_sdk.CstSdkSession(self.root, interface_module=self.h.interface,
                                         identity_probe=self.h.probe)
        self.assertEqual(self.h.calls, [])
        self.assertEqual(self.h.probed, [])

    def test_lazy_module_import_has_no_cst_import(self):
        with mock.patch.object(importlib, "import_module", side_effect=AssertionError("vendor import")):
            importlib.reload(native_sdk)
        self.assertNotIn("cst.interface", sys.modules)

    def test_documented_complete_chain_and_injected_evidence(self):
        session = self.saved()
        self.assertEqual([call[0] for call in self.h.calls[:3]], ["new", "pid", "new_mws"])
        self.assertEqual(session.evidence_kind, "injected_test_interface")
        self.assertEqual(session.project_path, self.root.resolve() / "model.cst")
        session.add_to_history("geometry", "With Brick\nEnd With", .3)
        session.execute_vba("Sub Main\nEnd Sub", .3)
        session.start_solver(.3)
        self.assertTrue(session.is_solver_running(.3))
        session.abort_solver(.3)
        self.assertFalse(session.is_solver_running(.3))
        self.assertEqual(session.get_solver_run_info(.3), self.h.solver_info)
        session.save(include_results=True, timeout_seconds=.3)
        session.close_project(.3)
        receipt = session.close(.3)
        self.assertTrue(receipt["owned_closed"])
        self.assertEqual(receipt["evidence_kind"], "injected_test_interface")
        saves = [call for call in self.h.calls if call[0] == "save"]
        self.assertEqual([call[-1] for call in saves], [False, True])
        self.assertEqual([call[0] for call in self.h.calls].count("project_close"), 1)
        self.assertEqual(self.h.calls[-1], ("de_close",))
        self.assertTrue(all(pid == 811 for pid in self.h.probed))

    def test_probe_and_runtime_ownership_match_full_identity(self):
        session = self.session()
        recorded, observed = session.ownership()
        self.assertEqual(recorded, observed)
        self.assertEqual(recorded["run_dir"], str(self.root.resolve()))
        self.assertTrue(recorded["session_id"])
        self.assertEqual(recorded["created_at"], "filetime:123456")
        recorded["created_at"] = "tamper"
        self.assertEqual(session.ownership()[0]["created_at"], "filetime:123456")

    def test_new_environment_scratch_is_owned_without_global_env_mutation(self):
        original_temp = os.environ.get("TEMP")
        original_tmp = os.environ.get("TMP")
        with mock.patch.dict(os.environ, {"CST_FAKE_MARKER": "injected-marker"}):
            session = self.session()
            self.assertIsNotNone(self.h.temp_environment)
            self.assertEqual(self.h.temp_environment["CST_FAKE_MARKER"], "injected-marker")
            self.assertEqual(self.h.temp_environment["TEMP"], str(session.run_dir / "temp"))
            self.assertEqual(self.h.temp_environment["TMP"], str(session.run_dir / "temp"))
            self.assertTrue((session.run_dir / "temp").is_dir())
            self.assertEqual(os.environ.get("TEMP"), original_temp)
            self.assertEqual(os.environ.get("TMP"), original_tmp)

    def test_preexisting_project_rejects_without_launch(self):
        (self.root / "model.cst").write_bytes(b"foreign")
        with self.assertRaises((ValueError, FileExistsError)):
            self.session()
        self.assertEqual(self.h.calls, [])
        self.assertEqual((self.root / "model.cst").read_bytes(), b"foreign")

    def test_first_save_refuses_file_created_after_session_creation(self):
        session = self.session()
        (self.root / "model.cst").write_bytes(b"foreign")
        with self.assertRaises((ValueError, FileExistsError)):
            session.save(timeout_seconds=.3)
        self.assertNotIn("save", [call[0] for call in self.h.calls])

    def test_save_passes_string_path_to_strict_sdk_interface(self):
        session = self.session()
        original_save = self.h.project.save
        def strict_save(path="", include_results=True, allow_overwrite=False):
            self.assertIsInstance(path, str, "injected strict SDK save expects str path")
            return original_save(path, include_results=include_results,
                                 allow_overwrite=allow_overwrite)
        self.h.project.save = strict_save
        session.save(timeout_seconds=.3)
        self.assertTrue(session.project_path.is_file())
        self.assertEqual(self.h.project.filename(), str(session.project_path))

    def test_readonly_project_path_cannot_redirect_save(self):
        session = self.session()
        with self.assertRaises((AttributeError, ValueError)):
            session.project_path = self.root.parent / "escaped.cst"
        session.save(timeout_seconds=.3)
        self.assertFalse((self.root.parent / "escaped.cst").exists())

    def test_saved_filename_change_blocks_owned_overwrite(self):
        session = self.saved()
        self.h.project.saved_filename = str(self.root.parent / "foreign.cst")
        count = len(self.h.calls)
        with self.assertRaises(ValueError):
            session.save(timeout_seconds=.3)
        self.assertEqual(len(self.h.calls), count)

    def test_saved_project_hardlink_replacement_never_overwrites_foreign_file(self):
        session = self.saved()
        foreign = self.root.parent / "foreign.cst"
        foreign.write_bytes(b"foreign test file")
        session.project_path.unlink()
        os.link(foreign, session.project_path)
        count = len(self.h.calls)
        with self.assertRaises(ValueError):
            session.save(timeout_seconds=.3)
        self.assertEqual(len(self.h.calls), count)
        self.assertEqual(foreign.read_bytes(), b"foreign test file")

    def test_saved_project_regular_file_replacement_blocks_overwrite(self):
        session = self.saved()
        foreign = self.root.parent / "foreign.cst"
        foreign.write_bytes(b"foreign test file")
        os.replace(foreign, session.project_path)
        count = len(self.h.calls)
        with self.assertRaises(ValueError):
            session.save(timeout_seconds=.3)
        self.assertEqual(len(self.h.calls), count)
        self.assertEqual(session.project_path.read_bytes(), b"foreign test file")

    def test_owned_path_escape_after_creation_blocks_sdk_save(self):
        session = self.session()
        session.run_dir = self.root.parent.resolve()
        count = len(self.h.calls)
        with self.assertRaises(ValueError):
            session.save(timeout_seconds=.3)
        self.assertEqual(len(self.h.calls), count)
        self.assertFalse((self.root.parent / "model.cst").exists())

    def test_initial_observer_cannot_claim_foreign_run_identity(self):
        self.h.observation["run_dir"] = str(self.root.parent.resolve())
        with self.assertRaises(ValueError):
            self.session()
        self.assertNotIn("new_mws", [call[0] for call in self.h.calls])
        self.assertNotIn("de_close", [call[0] for call in self.h.calls])

    def test_incomplete_initial_identity_never_creates_project_or_closes(self):
        del self.h.observation["created_at"]
        with self.assertRaises(ValueError):
            self.session()
        self.assertNotIn("new_mws", [call[0] for call in self.h.calls])
        self.assertNotIn("de_close", [call[0] for call in self.h.calls])

    def test_every_mutation_blocks_reused_pid(self):
        for name in ("save", "add_to_history", "execute_vba", "start_solver", "abort_solver", "close_project"):
            with self.subTest(method=name):
                self.h = Harness(self.root)
                if (self.root / "model.cst").exists():
                    (self.root / "model.cst").unlink()
                session = self.saved()
                self.h.observation["created_at"] = "filetime:another"
                count = len(self.h.calls)
                arguments = {"save": (), "add_to_history": ("x", "x", .3),
                             "execute_vba": ("x", .3), "start_solver": (.3,),
                             "abort_solver": (.3,), "close_project": (.3,)}[name]
                with self.assertRaises(ValueError):
                    getattr(session, name)(*arguments)
                self.assertEqual(len(self.h.calls), count)

    def test_foreign_executable_prevents_close(self):
        session = self.saved()
        self.h.observation["executable"] = str(self.root / "foreign.exe")
        count = len(self.h.calls)
        self.assertFalse(session.close(.1)["owned_closed"])
        self.assertEqual(len(self.h.calls), count)

    def test_unknown_or_gone_with_old_identity_never_authorizes_mutation(self):
        session = self.saved()
        recorded, _ = session.ownership()
        count = len(self.h.calls)
        for state in ("unknown", "gone"):
            self.h.observation = {**recorded, "state": state}
            with self.subTest(state=state):
                with self.assertRaises(ValueError):
                    session.add_to_history("foreign", "code", .3)
                self.assertEqual(len(self.h.calls), count)

    def test_unknown_with_changed_token_never_proves_pid_reuse(self):
        session = self.saved()
        recorded, _ = session.ownership()
        de = self.h.de
        original_close = de.close
        def close_then_unknown():
            original_close()
            self.h.observation = {**recorded, "state": "unknown", "created_at": "filetime:999999"}
        de.close = close_then_unknown
        self.assertFalse(session.close(.1)["owned_closed"])

    def test_foreign_cleanup_identity_never_invokes_sdk(self):
        session = self.saved()
        recorded, _ = session.ownership()
        recorded["session_id"] = "foreign"
        count = len(self.h.calls)
        self.assertFalse(session.cleanup_identity(recorded)["owned_closed"])
        self.assertEqual(len(self.h.calls), count)

    def test_owned_cleanup_identity_rechecks_and_closes(self):
        session = self.saved()
        self.assertTrue(session.cleanup_identity(session.ownership()[0])["owned_closed"])

    def test_close_aborts_only_owned_running_solver_before_project_and_de(self):
        session = self.saved()
        session.start_solver(.3)
        self.assertTrue(session.close(.3)["owned_closed"])
        names = [call[0] for call in self.h.calls]
        self.assertLess(names.index("abort"), names.index("project_close"))
        self.assertLess(names.index("project_close"), names.index("de_close"))

    def test_project_close_refuses_unsaved_or_running_project(self):
        session = self.session()
        with self.assertRaises(ValueError):
            session.close_project(.3)
        session.save(timeout_seconds=.3)
        session.start_solver(.3)
        with self.assertRaises(ValueError):
            session.close_project(.3)
        self.assertNotIn("project_close", [call[0] for call in self.h.calls])

    def test_abort_that_does_not_stop_solver_never_closes_project_or_de(self):
        session = self.saved()
        session.start_solver(.3)
        self.h.abort_stuck = True
        self.assertFalse(session.close(.06)["owned_closed"])
        self.assertNotIn("project_close", [call[0] for call in self.h.calls])
        self.assertNotIn("de_close", [call[0] for call in self.h.calls])

    def test_close_failure_or_access_denied_never_claims_closed(self):
        for mode in ("error", "alive", "unknown"):
            with self.subTest(mode=mode):
                self.h = Harness(self.root)
                (self.root / "model.cst").unlink(missing_ok=True)
                session = self.saved()
                self.h.close_mode = mode
                self.assertFalse(session.close(.06)["owned_closed"])

    def test_project_close_failure_does_not_close_environment(self):
        session = self.saved()
        self.h.project_close_error = True
        self.assertFalse(session.close(.1)["owned_closed"])
        self.assertNotIn("de_close", [call[0] for call in self.h.calls])

    def test_pid_reused_after_environment_close_confirms_original_gone(self):
        session = self.saved()
        self.h.close_mode = "reused"
        self.assertTrue(session.close(.3)["owned_closed"])

    def test_timed_out_close_can_confirm_after_original_call_finishes(self):
        session = self.saved()
        self.h.de_close_gate = threading.Event()
        self.addCleanup(self.h.de_close_gate.set)
        self.assertFalse(session.close(.2)["owned_closed"])
        self.assertIn("de_close", [call[0] for call in self.h.calls])
        self.h.de_close_gate.set()
        self.assertTrue(self.h.observed_gone.wait(.5))
        deadline = time.monotonic() + .5
        receipt = session.close(.3)
        while not receipt["owned_closed"] and time.monotonic() < deadline:
            time.sleep(.002)
            receipt = session.close(.3)
        self.assertTrue(receipt["owned_closed"])
        self.assertEqual([c[0] for c in self.h.calls].count("de_close"), 1)

    def test_confirmed_closure_cannot_be_downgraded_by_caller_timeout(self):
        session = self.saved()
        release = threading.Event()
        self.addCleanup(release.set)
        original = session._close_body
        def switch_after_confirmed(deadline):
            receipt = original(deadline)
            release.wait(1)
            return receipt
        with mock.patch.object(session, "_close_body", switch_after_confirmed):
            self.assertFalse(session.close(.2)["owned_closed"])
            self.assertTrue(self.h.observed_gone.is_set())
            release.set()
            deadline = time.monotonic() + .5
            receipt = session.close(.3)
            while not receipt["owned_closed"] and time.monotonic() < deadline:
                time.sleep(.002)
                receipt = session.close(.3)
            self.assertTrue(receipt["owned_closed"])
        self.assertEqual([c[0] for c in self.h.calls].count("de_close"), 1)

    def test_pending_history_timeout_never_races_sdk_cleanup(self):
        session = self.saved()
        self.h.history_gate = threading.Event()
        self.addCleanup(self.h.history_gate.set)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            session.add_to_history("slow", "code", .03)
        self.assertLess(time.monotonic() - started, .25)
        self.assertFalse(session.close(.03)["owned_closed"])
        self.assertNotIn("de_close", [call[0] for call in self.h.calls])
        self.h.history_gate.set()

    def test_solver_info_retains_raw_schema_and_rejects_non_dict(self):
        session = self.saved()
        session.is_solver_running(.3)
        self.h.solver_info = {"unknown-native-version-key": "preserved"}
        self.assertEqual(session.get_solver_run_info(.3), self.h.solver_info)
        self.h.solver_info = "invalid native return"
        with self.assertRaises(ValueError):
            session.get_solver_run_info(.3)

    def test_native_timeout_units_are_not_guessed(self):
        session = self.saved()
        session.add_to_history("h", "c", .3)
        session.execute_vba("c", .3)
        session.start_solver(.3)
        session.is_solver_running(.3)
        session.abort_solver(.3)
        session.get_solver_run_info(.3)
        for call in self.h.calls:
            if call[0] in ("history", "vba", "start", "poll", "abort", "solver_info"):
                self.assertIsNone(call[-1])

    def test_invalid_timeout_prevents_mutation(self):
        session = self.saved()
        for timeout in (0, -1, float("nan"), float("inf"), True, "1"):
            count = len(self.h.calls)
            with self.assertRaises(ValueError):
                session.start_solver(timeout)
            self.assertEqual(len(self.h.calls), count)

    def test_cancellation_type_preserves_safety_error_parent_contract(self):
        self.assertTrue(hasattr(native_sdk, "SessionCancelled"), "transport needs a typed cancellation")
        self.assertTrue(issubclass(native_sdk.SessionCancelled, native_sdk.SessionSafetyError))
        self.assertTrue(issubclass(native_sdk.SessionCancelled, ValueError))

    def test_prestartup_and_active_stop_raise_typed_cancel_without_sdk_mutation(self):
        self.assertTrue(hasattr(native_sdk, "SessionCancelled"), "transport needs a typed cancellation")
        stop = threading.Event()
        stop.set()
        with self.assertRaises(native_sdk.SessionCancelled):
            self.session(stop_event=stop)
        self.assertEqual(self.h.calls, [])
        stop.clear()
        session = self.session(stop_event=stop)
        session.save(timeout_seconds=.3)
        stop.set()
        count = len(self.h.calls)
        for method, arguments in (("save", ()), ("add_to_history", ("h", "c", .3)),
                                  ("execute_vba", ("c", .3)), ("start_solver", (.3,))):
            with self.subTest(method=method):
                with self.assertRaises(native_sdk.SessionCancelled):
                    getattr(session, method)(*arguments)
        self.assertEqual(len(self.h.calls), count)

    def test_foreign_identity_observed_during_stop_remains_safety_failure(self):
        self.assertTrue(hasattr(native_sdk, "SessionCancelled"), "transport needs a typed cancellation")
        stop = threading.Event()
        trigger = [False]
        def probe(pid):
            if trigger[0]:
                self.h.observation["created_at"] = "filetime:foreign"
                stop.set()
            return self.h.probe(pid)
        session = native_sdk.CstSdkSession(self.root, authorized=True,
                                           interface_module=self.h.interface,
                                           identity_probe=probe, stop_event=stop)
        session.save(timeout_seconds=.3)
        count = len(self.h.calls)
        trigger[0] = True
        with self.assertRaises(native_sdk.SessionSafetyError) as raised:
            session.start_solver(.3)
        self.assertNotIsInstance(raised.exception, native_sdk.SessionCancelled)
        self.assertEqual(len(self.h.calls), count)

    def test_deadline_and_path_failures_keep_distinct_types_when_stop_is_set(self):
        self.assertTrue(hasattr(native_sdk, "SessionCancelled"), "transport needs a typed cancellation")
        stop = threading.Event()
        session = self.session(stop_event=stop)
        stop.set()
        with self.assertRaises(native_sdk.SessionTimeout) as raised:
            session._deadline(time.monotonic() - 1)
        self.assertNotIsInstance(raised.exception, native_sdk.SessionCancelled)
        with self.assertRaises(native_sdk.SessionCancelled):
            session._deadline(time.monotonic() + 1)
        session.run_dir = self.root.parent.resolve()
        with self.assertRaises(native_sdk.SessionSafetyError) as raised:
            session.add_to_history("h", "c", .3)
        self.assertNotIsInstance(raised.exception, native_sdk.SessionCancelled)

    def test_startup_handoff_stop_is_typed_cancel_with_owned_late_cleanup(self):
        self.assertTrue(hasattr(native_sdk, "SessionCancelled"), "transport needs a typed cancellation")
        stop = threading.Event()
        original = native_sdk.CstSdkSession._startup_stopped
        checks = [0]
        def stop_after_final_check(session, deadline):
            result = original(session, deadline)
            checks[0] += 1
            if checks[0] == 4:
                stop.set()
            return result
        with mock.patch.object(native_sdk.CstSdkSession, "_startup_stopped", stop_after_final_check):
            with self.assertRaises(native_sdk.SessionCancelled) as raised:
                self.session(stop_event=stop, startup_timeout_seconds=.5)
        self.assertIsNotNone(raised.exception.session.identity)
        self.assertTrue(self.h.de_closed.wait(.5))

    def test_ready_startup_deadline_error_is_forwarded_as_timeout_not_cancel(self):
        session = self.session()
        with self.assertRaises((native_sdk.SessionTimeout, native_sdk.SessionCancelled)) as raised:
            session._request("deadline-classification",
                             lambda _: session._startup(self.h.interface, time.monotonic() - 1),
                             .3, startup=True)
        self.assertIsInstance(raised.exception, native_sdk.SessionTimeout)
        self.assertNotIsInstance(raised.exception, native_sdk.SessionCancelled)

    def test_stop_event_before_startup_prevents_launch(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(ValueError):
            self.session(stop_event=stop, startup_timeout_seconds=.1)
        self.assertEqual(self.h.calls, [])

    def test_stop_during_guard_probe_prevents_solver_start(self):
        self.assertTrue(hasattr(native_sdk, "SessionCancelled"), "transport needs a typed cancellation")
        stop = threading.Event()
        entered = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        blocking = [False]
        def probe(pid):
            if blocking[0]:
                entered.set()
                release.wait(1)
            return self.h.probe(pid)
        session = native_sdk.CstSdkSession(self.root, authorized=True,
                                           interface_module=self.h.interface,
                                           identity_probe=probe, stop_event=stop)
        session.save(timeout_seconds=.3)
        blocking[0] = True
        errors = []
        def start():
            try:
                session.start_solver(.5)
            except ValueError as error:
                errors.append(error)
        thread = threading.Thread(target=start, daemon=True)
        thread.start()
        self.assertTrue(entered.wait(.3))
        stop.set()
        release.set()
        thread.join(.5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)
        self.assertIsInstance(errors[0], native_sdk.SessionCancelled)
        self.assertNotIn("start", [call[0] for call in self.h.calls])

    def test_late_new_after_timeout_closes_only_fresh_environment(self):
        self.h.new_gate = threading.Event()
        self.addCleanup(self.h.new_gate.set)
        with self.assertRaises(TimeoutError):
            self.session(startup_timeout_seconds=.03)
        self.assertNotIn("de_close", [call[0] for call in self.h.calls])
        self.h.new_gate.set()
        self.assertTrue(self.h.de_closed.wait(.5))
        self.assertNotIn("new_mws", [call[0] for call in self.h.calls])
        self.assertEqual(self.h.calls[-1], ("de_close",))

    def test_stop_event_during_new_closes_fresh_environment_without_new_project(self):
        self.assertTrue(hasattr(native_sdk, "SessionCancelled"), "transport needs a typed cancellation")
        stop = threading.Event()
        self.h.new_gate = threading.Event()
        self.addCleanup(self.h.new_gate.set)
        observed = []
        def create():
            try:
                self.session(stop_event=stop, startup_timeout_seconds=.5)
            except ValueError as error:
                observed.append(error)
        thread = threading.Thread(target=create, daemon=True)
        thread.start()
        for _ in range(100):
            if self.h.calls:
                break
            time.sleep(.001)
        stop.set()
        self.h.new_gate.set()
        thread.join(.5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(observed)
        self.assertIsInstance(observed[0], native_sdk.SessionCancelled)
        self.assertTrue(self.h.de_closed.is_set())
        self.assertNotIn("new_mws", [call[0] for call in self.h.calls])

    def test_startup_guard_probe_crossing_timeout_never_creates_project(self):
        release = threading.Event()
        self.addCleanup(release.set)
        calls = [0]
        def probe(pid):
            calls[0] += 1
            if calls[0] == 2:
                release.wait(1)
            return self.h.probe(pid)
        with self.assertRaises(TimeoutError):
            native_sdk.CstSdkSession(self.root, authorized=True, interface_module=self.h.interface,
                                     identity_probe=probe, startup_timeout_seconds=.2)
        release.set()
        self.assertTrue(self.h.de_closed.wait(.5))
        self.assertNotIn("new_mws", [call[0] for call in self.h.calls])

    def test_startup_path_check_crossing_timeout_never_launches_environment(self):
        release = threading.Event()
        self.addCleanup(release.set)
        original = native_sdk.CstSdkSession._check_path
        calls = [0]
        def slow_path_check(session):
            original(session)
            calls[0] += 1
            if calls[0] == 1:
                release.wait(1)
        with mock.patch.object(native_sdk.CstSdkSession, "_check_path", slow_path_check):
            with self.assertRaises(TimeoutError) as raised:
                self.session(startup_timeout_seconds=.2)
            release.set()
            raised.exception.session._active_thread.join(.5)
        self.assertEqual(self.h.calls, [])
        self.assertEqual(self.h.probed, [])

    def test_timeout_after_final_startup_check_still_cleans_fresh_environment(self):
        release = threading.Event()
        self.addCleanup(release.set)
        checks = [0]
        entered = threading.Event()
        original = native_sdk.CstSdkSession._startup_stopped
        def switch_after_check(session, deadline):
            value = original(session, deadline)
            checks[0] += 1
            if checks[0] == 4:
                entered.set()
                release.wait(1)
            return value
        with mock.patch.object(native_sdk.CstSdkSession, "_startup_stopped", switch_after_check):
            with self.assertRaises(TimeoutError):
                self.session(startup_timeout_seconds=.2)
            self.assertTrue(entered.is_set())
            release.set()
            self.assertTrue(self.h.de_closed.wait(.5))


if __name__ == "__main__":
    unittest.main()
