"""CLI acceptance using entirely synthetic geometry/materials and exports."""
import csv
import copy
import argparse
from contextlib import redirect_stdout
import importlib.util
import io
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
CLI = REPO / "scripts" / "absorber_cli.py"
sys.path.insert(0, str(REPO / "scripts"))


def config_fixture():
    return {"schema_version": "1.0", "case": {
        "id": "synthetic", "geometry": {
            "cell": {"Lx_m": .01, "Ly_m": .01, "height_m": .002},
            "regions": [{"id": "layer", "material": "demo", "kind": "brick",
                         "bounds_m": [[0, 0, 0], [.01, .01, .001]]}]},
        "materials": {"demo": {"time_convention": "exp(+jωt)", "frequency_unit": "GHz",
            "epsilon_mu_table": [{"frequency": f, "epsilon_real": 2, "epsilon_imag": -.1,
                                  "mu_real": 1, "mu_imag": 0} for f in [1, 2]],
            "measured_ranges_Hz": {"epsilon": [1e9, 2e9], "mu": [1e9, 2e9]},
            "density_kg_m3": 1000}},
        "scenario": {"profile": "periodic-pec", "frequencies_Hz": [1e9, 1.5e9, 2e9],
                     "theta_deg": 0, "azimuth_deg": 0, "polarization": "TE",
                     "air_height_m": .01, "reference_plane_m": .005},
        "mesh": {"max_edge_m": .0005},
        "analysis": {"band_Hz": [1e9, 2e9], "max_gap_Hz": 5e8}},
        "runtime": {"max_cpus": 1, "min_available_RAM_GiB": 0,
                    "min_free_disk_GiB": 0, "max_modes": 16,
                    "wall_budget_seconds": 120}}


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "case.json"
        self.write_config(config_fixture())

    def write_config(self, raw):
        self.config.write_text(json.dumps(raw), encoding="utf-8")

    def cli(self, *args, success=True):
        self.assertTrue(CLI.is_file(), "offline CLI commands must be implemented")
        proc = subprocess.run([sys.executable, str(CLI), *map(str, args)],
                              capture_output=True, text=True, cwd=REPO)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(proc.returncode, 0 if success else 2, proc.stdout + proc.stderr)
        self.assertTrue(proc.stdout.strip(), "CLI command must produce structured JSON output: " + proc.stderr)
        return json.loads(proc.stdout)

    def exports(self):
        spectra, power = [], []
        for f, r in zip([1e9, 1.5e9, 2e9], [.2, .05, .01]):
            for polarization in ["TE", "TM"]:
                spectra.append({"frequency_Hz": f, "m": 0, "n": 0,
                    "polarization": polarization, "s_re": math.sqrt(r) if polarization == "TE" else 0,
                    "s_im": 0, "gamma_re_per_m": 0,
                    "gamma_im_per_m": 2 * math.pi * f / 299792458,
                    "normalization": "power"})
            power.append({"frequency_Hz": f, "incident_W": 1, "reflected_W": r,
                          "absorbed_W": 1 - r, "transmitted_W": 0})
        for name, rows in [("synthetic_spectra.csv", spectra), ("synthetic_power.csv", power)]:
            with (self.root / name).open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        return self.root / "synthetic_spectra.csv", self.root / "synthetic_power.csv"

    def test_validate_multiple_frequencies_stay_one_case(self):
        result = self.cli("validate", self.config)
        self.assertEqual(result["case_count"], 1)
        self.assertEqual(result["scope_mode"], "single")
        self.assertEqual(result["CST_execution"], "not_run")

    def test_plan_writes_original_prepared_inputs_and_modal_requirements(self):
        target = self.root / "plan.json"
        result = self.cli("plan", self.config, "--output", target)
        plan = json.loads(target.read_text())
        self.assertEqual(plan["scope_mode"], "single")
        self.assertEqual(len(plan["cases"]), 1)
        self.assertIn("required_modes", plan["cases"][0])
        self.assertIn("geometry_audit", plan["cases"][0])
        self.assertEqual(result["CST_execution"], "not_run")

    def test_unconfirmed_batch_and_implicit_angle_scan_fail(self):
        raw = config_fixture()
        raw["cases"] = [raw.pop("case"), dict(config_fixture()["case"], id="second")]
        self.write_config(raw)
        self.assertEqual(self.cli("validate", self.config, success=False)["status"], "error")
        raw = config_fixture()
        raw["case"]["scenario"]["theta_deg"] = [0, 15]
        self.write_config(raw)
        self.cli("plan", self.config, success=False)

    def test_confirmed_batch_requires_explicit_case_for_analysis(self):
        raw = config_fixture()
        raw["execution"] = {"mode": "batch", "confirmed": True}
        raw["cases"] = [raw.pop("case"), dict(config_fixture()["case"], id="second")]
        self.write_config(raw)
        spectra, power = self.exports()
        error = self.cli("analyze", self.config, "--spectra", spectra, "--power", power,
                         "--out-dir", self.root / "analysis", success=False)
        self.assertIn("case-id", error["message"])

    def test_analyze_generates_metrics_csv_plot_script_png_svg_without_certification(self):
        spectra, power = self.exports()
        result = self.cli("analyze", self.config, "--spectra", spectra, "--power", power,
                          "--out-dir", self.root / "analysis", "--export-origin", "synthetic")
        metrics = json.loads(Path(result["metrics"]).read_text())
        self.assertEqual(metrics["status"], "screening_only")
        self.assertFalse(metrics["numerically_qualified"])
        self.assertEqual(metrics["CST_execution"], "not_run")
        self.assertEqual(metrics["provenance"]["export_origin"], "synthetic")
        for key in ["csv", "script", "png", "svg"]:
            self.assertTrue(Path(result["plots"][key]).is_file(), key)

    def test_user_supplied_csv_never_claims_verified_cst_origin(self):
        spectra, power = self.exports()
        result = self.cli("analyze", self.config, "--spectra", spectra, "--power", power,
                          "--out-dir", self.root / "analysis")
        metrics = json.loads(Path(result["metrics"]).read_text())
        self.assertEqual(metrics["CST_execution"], "unverified_external")
        self.assertFalse(metrics["numerically_qualified"])

    def test_plot_standalone_metrics_file(self):
        path = self.root / "metrics.json"
        path.write_text(json.dumps({"frequency_Hz": [1e9, 2e9], "R": [.1, .01], "R00": [.1, .01],
            "RLtotal_dB": [-10, -20], "RL00_dB": [-10, -20], "zero_R": [False, False],
            "analysis": {"band_Hz": [1e9, 2e9], "max_gap_Hz": 1e9, "threshold_R": .1},
            "status": "screening_only", "numerically_qualified": False}), encoding="utf-8")
        result = self.cli("plot", path, "--out-dir", self.root / "plot")
        self.assertTrue(Path(result["plots"]["svg"]).is_file())

    def test_prepare_cst_emits_template_without_claiming_run(self):
        result = self.cli("prepare-cst", self.config, "--out-dir", self.root / "prepare")
        self.assertEqual(result["CST_execution"], "not_run")
        self.assertEqual(result["status"], "prepared")
        self.assertEqual(len(result["cases"]), 1)

    def test_live_run_and_resume_refuse_incomplete_authorization(self):
        target = self.root / "run"
        for command in ["run", "resume"]:
            result = self.cli(command, self.config, "--out-dir", target, "--backend", "cst",
                              "--authorize-live", success=False)
            self.assertEqual(result["CST_execution"], "not_run")
            self.assertIn("--exclusive-resources", result["message"])
        self.assertFalse(target.exists())

    def test_pause_and_status_never_launch_solver(self):
        target = self.root / "run"
        paused = self.cli("pause", target)
        self.assertEqual(paused["status"], "pause_requested")
        self.assertTrue((target / "pause.json").is_file())
        status = self.cli("status", target)
        self.assertEqual(status["CST_execution"], "unknown")
        self.assertEqual(status["status"], "paused")

    def test_status_preserves_external_backend_evidence_without_claiming_not_run(self):
        target = self.root / "external_run"
        target.mkdir()
        evidence = {"origin": "external_user_receipt", "validation": "unverified"}
        (target / "state.json").write_text(json.dumps({"schema_version": "1.0",
            "status": "paused", "inputs_hash": "synthetic", "backend_evidence": evidence,
            "cases": [{"id": "synthetic", "status": "pending", "attempts": []}]}), encoding="utf-8")
        result = self.cli("status", target)
        self.assertEqual(result["CST_execution"], "unknown")
        self.assertEqual(result["backend_evidence"], evidence)
        self.assertFalse(result["physical_certification"])

    def test_invalid_json_is_structured_error(self):
        self.config.write_text("{broken", encoding="utf-8")
        self.assertEqual(self.cli("validate", self.config, success=False)["status"], "error")

    def test_malformed_plot_metrics_are_structured_error_without_artifacts(self):
        path = self.root / "metrics.json"
        path.write_text(json.dumps({"status": "screening_only"}), encoding="utf-8")
        target = self.root / "plot"
        result = self.cli("plot", path, "--out-dir", target, success=False)
        self.assertEqual(result["status"], "error")
        self.assertIn("frequency_Hz", result["message"])
        self.assertFalse(target.exists())

    def test_plot_case_id_without_config_is_rejected_before_input_or_output(self):
        target = self.root / "plot"
        result = self.cli("plot", self.root / "missing.json", "--out-dir", target,
                          "--case-id", "synthetic", success=False)
        self.assertIn("requires --config", result["message"])
        self.assertFalse(target.exists())

    def test_help_lists_offline_commands_and_experimental_guard(self):
        self.assertTrue(CLI.is_file(), "CLI help must be implemented")
        proc = subprocess.run([sys.executable, str(CLI), "--help"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        for term in ["validate", "prepare-cst", "analyze", "experimental"]:
            self.assertIn(term, proc.stdout)

    def native_fixture(self):
        """Saved raw inventory constructed from synthetic numbers, never CST."""
        from cst_absorber.contracts import build_plan
        case = build_plan(json.loads(self.config.read_text()), self.root)["cases"][0]
        curves = []
        frequencies = [1e9, 1.5e9, 2e9]
        reflections = [.2, .05, .01]
        def curve(name, values, unit, title=None):
            treepath = ("synthetic/Excitation [Zmax(7)]/" if unit == "W" else "synthetic/") + name
            selector = {"treepath": treepath, "run_id": 3, "title": title or name,
                        "xlabel": "Frequency / Hz", "ylabel": name + " / " + unit,
                        "x_unit": "Hz", "x_to_Hz": 1, "y_unit": unit, "y_scale": 1}
            curves.append({**{key: selector[key] for key in ["treepath", "run_id", "title", "xlabel", "ylabel"]},
                "reported_treepath": selector["treepath"], "requested_run_id": 3,
                "kind": "1D", "finite": True, "identity_valid": True, "point_count": 3,
                "declared_point_count": 3, "analysis_eligible": True, "data_consistent": True,
                "invalid_reasons": [], "data_tuples_raw": list(map(list, zip(frequencies, values))),
                "ydata_raw": values, "reference_impedance": None,
                "reference_impedance_consistency": "not_present",
                "x": frequencies, "y": values, "parameters": {"theta": 0},
                "module_parameters": {"theta": 0}, "data_origin": "synthetic_demo_not_CST"})
            return selector
        modes, actual = [], []
        for index, polarization in [(7, "TE"), (9, "TM")]:
            reflection = curve("coefficient " + polarization,
                               [math.sqrt(r) if polarization == "TE" else 0 for r in reflections], "1",
                               title=f"SZmax({index}),Zmax(7)")
            gamma = curve("Gamma " + polarization,
                          [{"real": 0, "imag": 2 * math.pi * f / 299792458} for f in frequencies], "1/m")
            modes.append({"port": "Zmax", "native_mode_index": index, "mode_name": polarization + "(0,0)",
                          "m": 0, "n": 0, "polarization": polarization, "reflection": reflection, "gamma": gamma})
            actual.append({"port": "Zmax", "index": index, "name": polarization + "(0,0)",
                           "m": 0, "n": 0, "polarization": polarization})
        profile = {"schema_version": "cst-result-profile/1", "confirmed": True,
            "case_signature": case["signature"], "run_id": 3, "expected_parameters": {"theta": 0},
            "normalization": "power", "normalization_evidence": "synthetic declared source only",
            "excitation": {
                "incident_mode": {key: modes[0][key] for key in
                                  ["port", "native_mode_index", "mode_name", "m", "n", "polarization"]},
                "s_identity": {"field": "title", "template":
                               "S{receive_port}({receive_mode}),{incident_port}({incident_mode})"},
                "power_identity": {"field": "treepath", "template":
                                   "synthetic/Excitation [{incident_port}({incident_mode})]"}},
            "modes": modes, "power": {"stimulated": curve("Power Stimulated", [1, 1, 1], "W"),
                "reflected": curve("Power Reflected", reflections, "W"),
                "material_absorbed": [{"material_id": "demo", "selector":
                                       curve("Loss per Material demo", [1-r for r in reflections], "W")}]},
            "boundary_assumption": {"kind": "PEC", "boundary": "Zmin", "value": "electric"}}
        raw = {"schema_version": "cst-raw-results/1", "status": "completed", "errors": [], "curves": curves}
        model = {"status": "model_report_validated", "modes_port": "Zmax", "modes": actual,
                 "boundaries": {"Zmin": "electric", "Zmax": "open"}}
        paths = {}
        for name, value in [("raw", raw), ("profile", profile), ("model", model)]:
            paths[name] = self.root / (name + ".json")
            paths[name].write_text(json.dumps(value), encoding="utf-8")
        return paths

    def native_args(self, paths, output="native-analysis"):
        return ["analyze-native", self.config, "--raw-results", paths["raw"],
                "--model-readback", paths["model"], "--result-profile", paths["profile"],
                "--out-dir", self.root / output]

    def test_analyze_native_reuses_one_saved_case_and_remains_diagnostic_without_fit(self):
        result = self.cli(*self.native_args(self.native_fixture()))
        self.assertEqual(result["case_id"], "synthetic")
        self.assertEqual(result["status"], "diagnostic_only")
        self.assertEqual(result["CST_execution"], "unverified_saved_export")
        self.assertIs(result.get("physical_accepted"), False)
        identity = result["mapping_receipt"]["excitation_identity"]
        self.assertEqual(identity["status"], "validated_against_actual_labels")
        self.assertEqual(identity["incident_mode"]["native_mode_index"], 7)
        self.assertEqual(identity["incident_mode"]["polarization"], "TE")
        self.assertEqual(len(identity["s_columns"]), 2)
        self.assertEqual(len(identity["power_branches"]), 3)
        metrics = json.loads(Path(result["metrics"]).read_text())
        self.assertEqual(metrics["provenance"]["export_origin"], "unverified_saved_native_export")
        self.assertEqual(metrics["provenance"].get("transmission_source"), "read_back_PEC_boundary_assumption")
        self.assertIs(metrics["provenance"].get("transmission_exported"), False)
        self.assertFalse(metrics["numerically_qualified"])
        self.assertFalse(metrics["physical_accepted"])
        self.assertEqual(metrics["native_acceptance"], "not_run")
        self.assertIn("material_fit_readback", metrics["unresolved_gates"])
        self.assertEqual(metrics["frequency_Hz"], [1e9, 1.5e9, 2e9])
        for key in ["csv", "script", "png", "svg"]:
            self.assertTrue(Path(result["plots"][key]).is_file(), key)

    def test_analyze_native_failed_mapping_preserves_receipt_and_skips_analysis(self):
        paths = self.native_fixture()
        profile = json.loads(paths["profile"].read_text())
        profile["case_signature"] = "wrong synthetic signature"
        paths["profile"].write_text(json.dumps(profile), encoding="utf-8")
        result = self.cli(*self.native_args(paths), success=False)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["mapping_receipt"]["status"], "failed")
        self.assertTrue(Path(result["mapping_receipt_file"]).is_file())
        self.assertFalse((self.root / "native-analysis" / "metrics.json").exists())
        self.assertFalse(result["numerically_qualified"])

    def test_analyze_native_wrong_s_incidence_or_power_branch_fails_closed(self):
        paths = self.native_fixture()
        raw_original = json.loads(paths["raw"].read_text())
        profile_original = json.loads(paths["profile"].read_text())
        for name, expected_error in [("s-column", "incident column"),
                                     ("power-branch", "power excitation branch")]:
            with self.subTest(source=name):
                raw, profile = copy.deepcopy(raw_original), copy.deepcopy(profile_original)
                selector = (profile["modes"][1]["reflection"] if name == "s-column" else
                            profile["power"]["material_absorbed"][0]["selector"])
                curve = next(row for row in raw["curves"] if row["treepath"] == selector["treepath"])
                if name == "s-column":
                    # Labels agree with the profile, but the actual incident column is TM(9), not TE(7).
                    curve["title"] = selector["title"] = "SZmax(9),Zmax(9)"
                else:
                    curve["treepath"] = curve["reported_treepath"] = selector["treepath"] = (
                        "synthetic/Excitation [Zmax(9)]/Loss per Material demo")
                paths["raw"].write_text(json.dumps(raw), encoding="utf-8")
                paths["profile"].write_text(json.dumps(profile), encoding="utf-8")
                result = self.cli(*self.native_args(paths, name), success=False)
                receipt = result["mapping_receipt"]
                self.assertEqual(result["status"], "failed")
                self.assertEqual(receipt["status"], "failed")
                self.assertIn(expected_error, str(receipt["errors"]).lower())
                self.assertNotEqual(receipt["excitation_identity"]["status"], "validated_against_actual_labels")
                self.assertTrue(Path(result["mapping_receipt_file"]).is_file())
                self.assertIsNone(receipt["spectra_path"])
                self.assertIsNone(receipt["power_path"])
                for relative in ["mapped-results/spectra.csv", "mapped-results/power.csv", "metrics.json"]:
                    self.assertFalse((self.root / name / relative).exists(), relative)
                self.assertFalse(result["numerically_qualified"])
                self.assertFalse(result["physical_accepted"])

    def test_analyze_native_batch_requires_explicit_case_and_never_infers_cases(self):
        paths = self.native_fixture()
        raw = config_fixture()
        raw["execution"] = {"mode": "batch", "confirmed": True}
        raw["cases"] = [raw.pop("case"), dict(config_fixture()["case"], id="second")]
        self.write_config(raw)
        result = self.cli(*self.native_args(paths), success=False)
        self.assertIn("case-id", result["message"])
        self.assertFalse((self.root / "native-analysis").exists())
        result = self.cli(*self.native_args(paths), "--case-id", "synthetic")
        self.assertEqual(result["case_id"], "synthetic")
        self.assertEqual(result["status"], "diagnostic_only")

    def test_analyze_native_never_imports_backend_vendor_runtime_or_restarts_solver(self):
        paths = self.native_fixture()
        spec = importlib.util.spec_from_file_location("absorber_cli_saved_test", CLI)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original_import = __import__
        def guarded_import(name, *args, **kwargs):
            if name == "cst" or name.startswith("cst.") or name in (
                    "cst_absorber.backend", "cst_absorber.live_backend", "cst_absorber.runtime"):
                self.fail("saved export analysis must not import execution or resource/identity interfaces")
            return original_import(name, *args, **kwargs)
        output = io.StringIO()
        try:
            with patch("builtins.__import__", side_effect=guarded_import), redirect_stdout(output):
                code = module.main(list(map(str, self.native_args(paths))))
        except SystemExit as error:
            self.fail("offline saved-export command must parse: " + str(error))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "diagnostic_only")


class LiveCliDispatchTests(unittest.TestCase):
    """Dispatch-only tests inject every backend/controller action; no CST access."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "case.json"
        self.config.write_text(json.dumps(config_fixture()), encoding="utf-8")
        spec = importlib.util.spec_from_file_location("absorber_cli_test", CLI)
        self.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cli)

    def arguments(self, command="run", **overrides):
        values = {"command": command, "config": self.config, "out_dir": self.root / "run",
                  "backend": "cst", "authorize_live": True, "exclusive_resources": True,
                  "acceptance_run": True, "result_profile": None}
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_missing_gates_stop_before_backend_import_planning_and_output(self):
        omitted = [{"backend": None}, {"authorize_live": False}, {"exclusive_resources": False},
                   {"acceptance_run": False, "result_profile": None}]
        original_import = __import__
        def guarded_import(name, *args, **kwargs):
            if name == "cst_absorber.backend" or name.startswith("cst.interface"):
                self.fail("missing authorization must stop before backend/vendor import")
            return original_import(name, *args, **kwargs)
        for command in ["run", "resume"]:
            for missing in omitted:
                with self.subTest(command=command, missing=missing):
                    with patch("builtins.__import__", side_effect=guarded_import), patch.object(
                            self.cli, "prepared_plan", side_effect=AssertionError("planning before gates")):
                        result, code = self.cli.dispatch(self.arguments(command, **missing))
                    self.assertEqual(code, 2)
                    self.assertEqual(result["CST_execution"], "not_run")
                    self.assertIn("required_flags", result)
                    self.assertFalse((self.root / "run").exists())

    def test_acceptance_dispatch_preserves_failed_native_validation_pending_receipt(self):
        worker = argparse.Namespace(last_receipt={"status": "failed",
            "CST_execution": "native_run_unverified", "validation": "native_validation_pending",
            "backend_evidence": {"kind": "injected_test_interface", "native_acceptance": "not_run"},
            "numerically_qualified": False})
        seen = {}
        def controller(plan, root, backend, **kwargs):
            seen.update(plan=plan, root=root, backend=backend, kwargs=kwargs)
            return {"status": "failed", "backend_evidence": worker.last_receipt["backend_evidence"],
                    "physical_certification": False}
        with patch("cst_absorber.backend.CstBackend", return_value=worker) as constructor, patch(
                "cst_absorber.runtime.run_cases", side_effect=controller):
            result, code = self.cli.dispatch(self.arguments())
        self.assertEqual(code, 2)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["validation"], "native_validation_pending")
        self.assertEqual(result["CST_execution"], "native_run_unverified")
        self.assertFalse(result["numerically_qualified"])
        constructor.assert_called_once_with(authorized=True, exclusive_resources=True,
                                             acceptance_run=True, result_profile=None)
        self.assertEqual(seen["plan"]["scope_mode"], "single")
        self.assertEqual(len(seen["plan"]["cases"]), 1)
        self.assertIs(seen["backend"], worker)
        self.assertFalse(seen["kwargs"]["resume"])

    def test_profile_and_resume_dispatch_through_shared_controller(self):
        profile = self.root / "profile.json"
        mapping = {"schema_version": "1.0", "actual_leaf_mapping": "synthetic_test_only"}
        profile.write_text(json.dumps(mapping), encoding="utf-8")
        worker = argparse.Namespace(last_receipt={"status": "completed", "CST_execution": "injected_test_only",
            "validation": "not_run", "numerically_qualified": False})
        with patch("cst_absorber.backend.CstBackend", return_value=worker) as constructor, patch(
                "cst_absorber.runtime.run_cases", return_value={"status": "completed",
                    "backend_evidence": "injected_test_interface", "physical_certification": False}) as controller:
            result, code = self.cli.dispatch(self.arguments("resume", acceptance_run=False, result_profile=profile))
        constructor.assert_called_once_with(authorized=True, exclusive_resources=True,
                                             acceptance_run=False, result_profile=mapping)
        self.assertTrue(controller.call_args.kwargs["resume"])
        self.assertEqual(code, 0)
        self.assertEqual(result["CST_execution"], "injected_test_only")
        self.assertFalse(result["numerically_qualified"])

    def test_live_parser_accepts_future_execution_flags(self):
        tokens = ["run", str(self.config), "--out-dir", str(self.root / "run"), "--backend", "cst",
                  "--authorize-live", "--exclusive-resources", "--acceptance-run"]
        try:
            with redirect_stdout(io.StringIO()):
                parsed = self.cli.parser().parse_args(tokens)
        except SystemExit as error:
            self.fail("live CLI flags must parse: " + str(error))
        self.assertTrue(parsed.exclusive_resources)
        self.assertTrue(parsed.acceptance_run)


if __name__ == "__main__":
    unittest.main()
