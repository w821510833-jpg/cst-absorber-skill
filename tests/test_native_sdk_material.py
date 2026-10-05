"""Fixed formal material readback uses injected SDK/PID observations only."""
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from test_native_materials import material_text, OPERATION
from test_native_model import case_fixture
from test_native_sdk import Harness, native_sdk
from cst_absorber import native_materials


class NativeSdkMaterialReadbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "owned"
        self.root.mkdir()
        self.case = case_fixture()
        self.operation = "owned-material-operation-0001"
        self.h = Harness(self.root)
        self.session = native_sdk.CstSdkSession(
            self.root, authorized=True, interface_module=self.h.interface,
            identity_probe=self.h.probe, startup_timeout_seconds=2)
        self.session.save(timeout_seconds=2)
        self.target = self.session.run_dir / "native_materials.tsv"
        self.payload = material_text(self.case).replace(OPERATION, self.operation)
        self.after_vba = lambda: None
        def execute(code, /, timeout=None):
            self.h.calls.append(("vba", code, timeout))
            self.target.write_text(self.payload, encoding="utf-8")
            self.after_vba()
        self.h.project.schematic.execute_vba_code = execute

    def readback(self, operation=None, seconds=2):
        method = getattr(self.session, "execute_material_readback", None)
        self.assertTrue(callable(method), "trusted fixed material readback entrypoint is missing")
        return method(self.case, self.operation if operation is None else operation, timeout_seconds=seconds)

    def assert_not_published(self):
        self.assertNotIn(self.target, getattr(self.session, "_material_readback_outputs", {}))

    def test_fixed_getters_return_nonce_and_stable_sha_bound_to_owned_request(self):
        result = self.readback()
        code = next(call[1] for call in self.h.calls if call[0] == "vba")
        self.assertEqual(code, native_materials.material_readback_vba(
            self.case, self.root, operation_id=self.operation))
        self.assertIn("Material.GetSigma actualMaterial, sigmaX, sigmaY, sigmaZ", code)
        self.assertIn("Material.GetRho actualMaterial, rho", code)
        self.assertNotIn("On Error Resume Next", code)
        self.assertEqual(result["kind"], "material")
        self.assertEqual(result["operation_id"], self.operation)
        self.assertEqual(result["case_signature"], self.case["signature"])
        self.assertEqual(result["source_file"], "native_materials.tsv")
        self.assertEqual(result["source_sha256"], hashlib.sha256(self.target.read_bytes()).hexdigest())
        self.assertEqual(result["producer_binding"], "exact_owned_sdk_request_and_nonce")
        self.assertEqual(result["evidence_kind"], "injected_test_interface")
        self.assertIs(result["producer_verified"], False)
        self.assertIs(result["writer_identity_proven"], False)
        self.assertEqual(result["archive_integrity"]["state"], "pinned")
        self.assertEqual(self.session.verify_archive(2)["state"], "pinned")

    def test_caller_macro_text_is_not_accepted(self):
        self.assertTrue(callable(getattr(self.session, "execute_material_readback", None)),
                        "trusted fixed material readback entrypoint is missing")
        with self.assertRaises(TypeError):
            self.session.execute_material_readback(self.case, self.operation, code="SaveAs foreign")
        self.assertNotIn("vba", [call[0] for call in self.h.calls])

    def test_bad_nonce_is_rejected_before_vba(self):
        with self.assertRaises(ValueError):
            self.readback("injected\nSaveAs foreign")
        self.assertNotIn("vba", [call[0] for call in self.h.calls])

    def test_unowned_preexisting_report_is_not_overwritten(self):
        self.target.write_text(self.payload, encoding="utf-8")
        before = self.target.read_bytes()
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.readback()
        self.assertEqual(self.target.read_bytes(), before)
        self.assertNotIn("vba", [call[0] for call in self.h.calls])
        self.assert_not_published()

    def test_mismatched_output_nonce_is_not_published(self):
        self.payload = self.payload.replace(self.operation, "stale-prior-operation")
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.readback()
        self.assert_not_published()

    def test_unowned_output_created_during_schematic_capture_is_not_overwritten(self):
        schematic = self.h.project.schematic
        outside = b"outside file created after first destination check"
        def capture(project):
            self.target.write_bytes(outside)
            return schematic
        with mock.patch.object(type(self.h.project), "schematic", property(capture), create=True):
            with self.assertRaises(native_sdk.SessionSafetyError):
                self.readback()
        self.assertEqual(self.target.read_bytes(), outside)
        self.assertNotIn("vba", [call[0] for call in self.h.calls])
        self.assert_not_published()

    def test_unknown_pid_during_schematic_capture_blocks_macro_dispatch(self):
        schematic = self.h.project.schematic
        def capture(project):
            self.h.observation = {"state": "unknown", "pid": 811}
            return schematic
        with mock.patch.object(type(self.h.project), "schematic", property(capture), create=True):
            with self.assertRaises(native_sdk.SessionSafetyError):
                self.readback()
        self.assertNotIn("vba", [call[0] for call in self.h.calls])
        self.assert_not_published()

    def test_mismatched_output_case_is_not_published(self):
        self.payload = self.payload.replace(self.case["signature"], "0" * 64)
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.readback()
        self.assert_not_published()

    def test_duplicate_nonce_record_is_not_published(self):
        self.payload += "operation_id\t" + self.operation + "\n"
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.readback()
        self.assert_not_published()

    def test_reused_successful_nonce_is_rejected_before_second_vba(self):
        self.readback()
        count = len(self.h.calls)
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.readback()
        self.assertEqual(len(self.h.calls), count)

    def test_fresh_nonce_can_overwrite_only_unchanged_previously_owned_output(self):
        first = self.readback()
        self.payload = self.payload.replace(self.operation, "owned-material-operation-0002")
        second = self.readback("owned-material-operation-0002")
        self.assertNotEqual(first["source_sha256"], second["source_sha256"])
        self.assertEqual(second["operation_id"], "owned-material-operation-0002")
        self.assertEqual([call[0] for call in self.h.calls].count("vba"), 2)

    def test_replaced_owned_output_is_rejected_before_second_vba(self):
        self.readback()
        replacement = self.root / "outside.tmp"
        replacement.write_text(self.payload, encoding="utf-8")
        os.replace(replacement, self.target)
        count = len(self.h.calls)
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.readback("fresh-second-query")
        self.assertEqual(len(self.h.calls), count)

    def test_same_inode_owned_output_edit_is_rejected_before_second_vba(self):
        self.readback()
        self.target.write_text("outside in-place output changes", encoding="utf-8")
        count = len(self.h.calls)
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.readback("fresh-second-query")
        self.assertEqual(len(self.h.calls), count)

    def test_hardlinked_actual_output_is_not_published(self):
        foreign = self.root.parent / "foreign.tsv"
        foreign.write_text(self.payload, encoding="utf-8")
        def hardlink():
            self.target.unlink()
            os.link(foreign, self.target)
        self.after_vba = hardlink
        with self.assertRaises((ValueError, native_sdk.SessionSafetyError)):
            self.readback()
        self.assert_not_published()

    def test_linked_output_path_is_rejected_before_vba(self):
        original_linked = native_sdk._linked
        def linked(path):
            return Path(path) == self.target or original_linked(path)
        with mock.patch.object(native_sdk, "_linked", linked):
            with self.assertRaises(native_sdk.SessionSafetyError):
                self.readback()
        self.assertNotIn("vba", [call[0] for call in self.h.calls])
        self.assert_not_published()

    def test_archive_replacement_by_readback_blocks_publication(self):
        def archive_replace():
            replacement = self.root / "outside.cst"
            replacement.write_bytes(b"unattributed changed archive")
            os.replace(replacement, self.session.project_path)
        self.after_vba = archive_replace
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.readback()
        self.assert_not_published()
        self.assertEqual(self.session.archive_integrity()["state"], "quarantined")

    def test_wrong_sdk_filename_after_vba_blocks_publication(self):
        self.after_vba = lambda: setattr(self.h.project, "saved_filename", str(self.root.parent / "foreign.cst"))
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.readback()
        self.assert_not_published()

    def test_unknown_pid_after_vba_blocks_publication(self):
        self.after_vba = lambda: setattr(self.h, "observation", {"state": "unknown", "pid": 811})
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.readback()
        self.assert_not_published()

    def test_changed_output_between_stable_reads_is_not_published(self):
        original = native_materials._read_report
        changed = []
        def read(*args, **kwargs):
            result = original(*args, **kwargs)
            if not changed:
                changed.append(True)
                self.target.write_text(self.payload.replace("1000", "1001"), encoding="utf-8")
            return result
        with mock.patch.object(native_materials, "_read_report", read):
            with self.assertRaises(native_sdk.SessionSafetyError):
                self.readback()
        self.assertTrue(changed)
        self.assert_not_published()

    def test_stop_after_vba_blocks_publication(self):
        self.after_vba = self.session._stop_event.set
        with self.assertRaises(native_sdk.SessionCancelled):
            self.readback()
        self.assert_not_published()

    def test_timeout_late_vba_output_never_becomes_owned(self):
        release, entered = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.h.project.schematic.execute_vba_code
        def delayed(code, /, timeout=None):
            entered.set()
            release.wait(2)
            return original(code, timeout=timeout)
        self.h.project.schematic.execute_vba_code = delayed
        with self.assertRaises(native_sdk.SessionTimeout):
            self.readback(seconds=.15)
        self.assertTrue(entered.is_set())
        self.assertTrue(self.session.supervision_status()["request_pending"])
        release.set()
        self.session._active_thread.join(2)
        self.assert_not_published()
        count = len(self.h.calls)
        with self.assertRaises(native_sdk.SessionSafetyError):
            self.readback("fresh-query-after-timeout")
        self.assertEqual(len(self.h.calls), count)

    def test_material_acceptance_uses_only_in_memory_publication(self):
        original_request = self.session._request
        checked = []
        def request(label, function, seconds, **kwargs):
            accept = kwargs["on_accept"]
            def memory_only(prepared, deadline):
                checked.append(True)
                with mock.patch.object(native_materials, "_read_report", side_effect=AssertionError("read during accept")):
                    with mock.patch.object(self.session, "_guard", side_effect=AssertionError("PID/SDK during accept")):
                        with mock.patch.object(self.session, "_check_path", side_effect=AssertionError("FS during accept")):
                            return accept(prepared, deadline)
            kwargs["on_accept"] = memory_only
            return original_request(label, function, seconds, **kwargs)
        with mock.patch.object(self.session, "_request", request):
            self.readback()
        self.assertTrue(checked)


if __name__ == "__main__":
    unittest.main()
