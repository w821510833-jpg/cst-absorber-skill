#!/usr/bin/env python3
"""Absorber CLI with offline workflows and explicitly gated experimental CST execution."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile


def read_json(path):
    def invalid_constant(value):
        raise ValueError("non-finite JSON number: " + value)
    with Path(path).open(encoding="utf-8-sig") as handle:
        value = json.load(handle, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("JSON input must be an object")
    return value


def write_json(path, value):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".absorber-", suffix=".json", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, indent=2, ensure_ascii=True, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return str(path)


def prepared_plan(path):
    from cst_absorber.contracts import build_plan
    from cst_absorber.modal import required_modes
    path = Path(path).resolve()
    plan = build_plan(read_json(path), path.parent)
    for case in plan["cases"]:
        modes = required_modes(case)
        if len(modes) > plan["runtime"]["max_modes"]:
            raise ValueError("required modes exceed runtime.max_modes; increase the explicit limit")
        case["required_modes"] = modes
    return plan


def choose_case(plan, case_id):
    if case_id is None:
        if len(plan["cases"]) != 1:
            raise ValueError("batch analysis requires --case-id; one export pair belongs to one case")
        return plan["cases"][0]
    for case in plan["cases"]:
        if case["id"] == case_id:
            return case
    raise ValueError("--case-id does not match an explicitly configured case")


def parser():
    cli = argparse.ArgumentParser(description=(
        "Offline CST absorber preview: planning, CSV analysis and plots are available. "
        "Live CST run/resume are experimental and require explicit authorization, exclusive resources "
        "and acceptance-run or an explicit result profile; the native end-to-end trial failed "
        "and the current preview remains unqualified."))
    sub = cli.add_subparsers(dest="command", required=True)
    for command in ["validate", "plan", "prepare-cst", "run", "resume"]:
        command_parser = sub.add_parser(command)
        command_parser.add_argument("config", type=Path)
        if command == "plan":
            command_parser.add_argument("--output", type=Path, help="write prepared plan JSON")
        if command in ["prepare-cst", "run", "resume"]:
            command_parser.add_argument("--out-dir", type=Path, required=True)
        if command in ["run", "resume"]:
            command_parser.add_argument("--backend", choices=["cst"])
            command_parser.add_argument("--authorize-live", action="store_true",
                                        help="explicitly authorize this future live CST invocation")
            command_parser.add_argument("--exclusive-resources", action="store_true",
                                        help="declare reserved CPU/RAM/disk resources for this invocation")
            command_parser.add_argument("--acceptance-run", action="store_true",
                                        help="collect first native raw evidence; unresolved acceptance remains failed")
            command_parser.add_argument("--result-profile", type=Path,
                                        help="explicit actual-tree/run/unit/mode mapping JSON")
    analyze = sub.add_parser("analyze", help="analyze one explicit case and export metrics plus figures")
    analyze.add_argument("config", type=Path)
    analyze.add_argument("--spectra", type=Path, required=True)
    analyze.add_argument("--power", type=Path, required=True)
    analyze.add_argument("--out-dir", type=Path, required=True)
    analyze.add_argument("--case-id")
    analyze.add_argument("--export-origin", choices=["synthetic", "user_supplied_unverified"],
                         default="user_supplied_unverified",
                         help="source label; neither option certifies CST export provenance")
    analyze.add_argument("--style", type=Path, help="plot style JSON")
    saved = sub.add_parser("analyze-native", help="map/analyze/plot existing saved raw native exports offline")
    saved.add_argument("config", type=Path)
    saved.add_argument("--raw-results", type=Path, required=True)
    saved.add_argument("--model-readback", type=Path, required=True)
    saved.add_argument("--result-profile", type=Path, required=True)
    saved.add_argument("--out-dir", type=Path, required=True)
    saved.add_argument("--case-id")
    saved.add_argument("--style", type=Path, help="plot style JSON")
    plot = sub.add_parser("plot", help="plot an existing screening/diagnostic metrics JSON")
    plot.add_argument("result", type=Path)
    plot.add_argument("--out-dir", type=Path, required=True)
    plot.add_argument("--style", type=Path)
    plot.add_argument("--config", type=Path, help="optional material domains for figure annotation")
    plot.add_argument("--case-id")
    for command in ["pause", "status"]:
        controls = sub.add_parser(command)
        controls.add_argument("out_dir", type=Path)
    return cli


def dispatch(args):
    if args.command in ["run", "resume"]:
        missing = []
        for present, flag in [(args.backend == "cst", "--backend cst"),
                              (args.authorize_live, "--authorize-live"),
                              (args.exclusive_resources, "--exclusive-resources"),
                              (args.acceptance_run or args.result_profile is not None,
                               "--acceptance-run or --result-profile PATH")]:
            if not present:
                missing.append(flag)
        if missing:
            return {"status": "error", "CST_execution": "not_run", "capability_status": "experimental",
                    "numerically_qualified": False, "required_flags": missing,
                    "message": "Live CST execution requires " + ", ".join(missing)}, 2
        # All gates above precede config reads, output creation and backend imports.
        profile = read_json(args.result_profile) if args.result_profile is not None else None
        plan = prepared_plan(args.config)
        from cst_absorber.backend import CstBackend
        from cst_absorber.runtime import RunSupervisor, run_cases
        worker = CstBackend(authorized=True, exclusive_resources=True,
                            acceptance_run=args.acceptance_run, result_profile=profile)
        # A bounded controller return does not permit interpreter exit while
        # a native startup, request or owned close can still finish late.
        native_supervision = (callable(getattr(worker, "supervision_status", None))
                              and callable(getattr(worker, "supervise_cleanup", None)))
        supervisor = RunSupervisor() if native_supervision else None
        seen_warning = None
        def supervision_update(snapshot):
            nonlocal seen_warning
            if snapshot.get("requires_user_intervention") and snapshot.get("reason") != seen_warning:
                seen_warning = snapshot["reason"]
                print("CST supervision remains active: " + seen_warning +
                      ". Keep this interpreter open until owned closure is verified.",
                      file=sys.stderr, flush=True)
        options = {"resume": args.command == "resume"}
        if supervisor is not None:
            options["supervisor"] = supervisor
        supervision = None
        try:
            result = run_cases(plan, args.out_dir.resolve(), worker, **options)
        finally:
            if supervisor is not None and supervisor.started:
                supervision = supervisor.wait(worker, on_update=supervision_update)
        if supervision is not None:
            result.update(supervision=supervision, exit_ready=supervision["exit_ready"])
        receipt = getattr(worker, "last_receipt", None)
        if isinstance(receipt, dict):
            result["backend_receipt"] = receipt
            for key in ["CST_execution", "validation", "backend_evidence"]:
                if key in receipt:
                    result[key] = receipt[key]
        result.setdefault("CST_execution", "unknown")
        result["capability_status"] = "experimental"
        result["numerically_qualified"] = False
        result["physical_certification"] = False
        return result, 0 if result["status"] in ["completed", "paused"] else 2
    if args.command in ["pause", "status"]:
        from cst_absorber.runtime import request_pause, read_status
        if args.command == "pause":
            request_pause(args.out_dir)
            return {"status": "pause_requested", "CST_execution": "not_run",
                    "physical_certification": False}, 0
        result = read_status(args.out_dir)
        result.setdefault("CST_execution", "unknown")
        return result, 0 if result["status"] != "blocked" else 2
    if args.command == "plot":
        from cst_absorber.plotting import plot_rl
        if args.case_id and not args.config:
            raise ValueError("plot --case-id requires --config")
        result = read_json(args.result)
        if result.get("status") not in ["screening_only", "diagnostic_only"]:
            raise ValueError("plot requires screening_only or diagnostic_only metrics")
        if result.get("numerically_qualified", False):
            raise ValueError("preview cannot present numerically qualified results")
        for key in ["frequency_Hz", "R", "R00", "RLtotal_dB", "RL00_dB", "zero_R"]:
            if not isinstance(result.get(key), list):
                raise ValueError("plot metrics require list field " + key)
        if not isinstance(result.get("analysis"), dict) or "max_gap_Hz" not in result["analysis"]:
            raise ValueError("plot metrics require analysis.max_gap_Hz")
        case = choose_case(prepared_plan(args.config), args.case_id) if args.config else None
        paths = plot_rl(result, args.out_dir.resolve(), case,
                        read_json(args.style) if args.style else None)
        return {"status": result["status"], "numerically_qualified": False, "plots": paths}, 0
    plan = prepared_plan(args.config)
    summary = {"status": "validated", "scope_mode": plan["scope_mode"],
               "case_count": len(plan["cases"]), "content_hash": plan["content_hash"],
               "CST_execution": "not_run", "numerically_qualified": False}
    if args.command == "validate":
        return summary, 0
    if args.command == "plan":
        if args.output:
            summary.update(status="planned", plan_file=write_json(args.output, plan))
            return summary, 0
        return dict(plan, status="planned", CST_execution="not_run", numerically_qualified=False), 0
    if args.command == "prepare-cst":
        from cst_absorber.backend import prepare_cst
        receipts = []
        for index, case in enumerate(plan["cases"], start=1):
            receipt = prepare_cst(case, args.out_dir.resolve() / ("case_%04d" % index))
            receipts.append({"case_id": case["id"], "receipt": receipt})
        summary.update(status="prepared", cases=receipts)
        return summary, 0
    if args.command == "analyze-native":
        from cst_absorber.native_results import canonicalize_raw_results
        from cst_absorber.metrics import analyze_exports
        from cst_absorber.plotting import plot_rl
        case = choose_case(plan, args.case_id)
        raw = read_json(args.raw_results)
        model = read_json(args.model_readback)
        profile = read_json(args.result_profile)
        out_dir = args.out_dir.resolve()
        mapping_dir = out_dir / "mapped-results"
        mapping = canonicalize_raw_results(raw, profile, case, model, mapping_dir)
        response = {"status": mapping.get("status", "failed"), "case_id": case["id"],
                    "mapping_receipt": mapping, "mapping_receipt_file": str(mapping_dir / "mapping-receipt.json"),
                    "CST_execution": "unverified_saved_export", "export_origin": "unverified_saved_native_export",
                    "numerically_qualified": False, "physical_accepted": False,
                    "physical_certification": False, "native_acceptance": "not_run"}
        if (mapping.get("status") != "diagnostic_only" or not mapping.get("spectra_path")
                or not mapping.get("power_path")):
            response.update(status="failed", message="Saved native result mapping failed; receipt retained")
            return response, 2
        result = analyze_exports(case, Path(mapping["spectra_path"]), Path(mapping["power_path"]))
        if result.get("status") not in ["screening_only", "diagnostic_only"]:
            raise ValueError("analyzer returned unsupported result status")
        result.update(status="diagnostic_only", quality_state="diagnostic_only", numerically_qualified=False,
                      physical_accepted=False, physical_certification=False, native_acceptance="not_run",
                      CST_execution="unverified_saved_export", solver_evidence="unverified_saved_native_export",
                      unresolved_gates=sorted(set(mapping.get("unresolved_gates", []) + model.get("unresolved_gates", []))))
        result.setdefault("provenance", {}).update(export_origin="unverified_saved_native_export",
            export_origin_verified=False, mapping_evidence=mapping.get("mapping_evidence"),
            transmission_source=mapping.get("transmission_source"), transmission_exported=False,
            mapping_receipt_file=response["mapping_receipt_file"])
        response["metrics"] = write_json(out_dir / "metrics.json", result)
        response["plots"] = plot_rl(result, out_dir, case, read_json(args.style) if args.style else None)
        return response, 0
    if args.command == "analyze":
        from cst_absorber.metrics import analyze_exports
        from cst_absorber.plotting import plot_rl
        case = choose_case(plan, args.case_id)
        result = analyze_exports(case, args.spectra.resolve(), args.power.resolve())
        if result.get("status") not in ["screening_only", "diagnostic_only"]:
            raise ValueError("analyzer returned unsupported result status")
        result["numerically_qualified"] = False
        result["CST_execution"] = "not_run" if args.export_origin == "synthetic" else "unverified_external"
        result.setdefault("provenance", {}).update(export_origin=args.export_origin,
                                                   export_origin_verified=False)
        metrics_path = write_json(args.out_dir / "metrics.json", result)
        paths = plot_rl(result, args.out_dir.resolve(), case,
                        read_json(args.style) if args.style else None)
        return {"status": result["status"], "case_id": case["id"], "metrics": metrics_path,
                "plots": paths, "CST_execution": result["CST_execution"],
                "numerically_qualified": False}, 0
    raise ValueError("unsupported command")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result, code = dispatch(args)
    except (ValueError, TypeError, KeyError, OSError, ImportError, RuntimeError) as error:
        result, code = {"status": "error", "message": str(error), "numerically_qualified": False}, 2
    print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
