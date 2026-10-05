# Offline preview commands

Run from the skill folder with an existing Python environment providing NumPy,
Trimesh and Matplotlib. Offline commands do not install dependencies or import CST SDKs.
Use `python scripts/absorber_cli.py --help` for command syntax.

```text
python scripts/absorber_cli.py validate examples/single.json
python scripts/absorber_cli.py plan examples/single.json --output outputs/plan.json
python scripts/absorber_cli.py prepare-cst examples/single.json --out-dir outputs/prepared
python scripts/absorber_cli.py analyze examples/single.json --spectra examples/synthetic_spectra.csv --power examples/synthetic_power.csv --export-origin synthetic --out-dir outputs/synthetic
python scripts/absorber_cli.py analyze-native examples/single.json --raw-results inputs/resulttree.json --model-readback inputs/model-readback.json --result-profile inputs/actual_result_profile.json --out-dir outputs/saved_native_analysis
python scripts/absorber_cli.py plot outputs/synthetic/metrics.json --config examples/single.json --out-dir outputs/replot
python scripts/absorber_cli.py pause outputs/run
python scripts/absorber_cli.py status outputs/run
```

Paths to STL assets in a configuration resolve relative to that configuration file.
A single case can contain many regions and frequencies. The default remains one
case. Multiple angles, polarizations or parameter combinations must be explicit
cases in a confirmed batch; commands never construct a Cartesian scan. Analysis
of a confirmed batch requires `--case-id ID`, because one CSV pair belongs to one
case. A confirmed one-item list still follows the single-case path.

`analysis.threshold_R` defaults to 0.1, corresponding to -10 dB because
RL=10log10(R). A custom threshold uses that threshold's own dB value for both
total and longest continuous bandwidth; do not label a custom-threshold width
as -10 dB bandwidth.

`plan` prints the prepared JSON when `--output` is omitted. It includes independent
modal requirements and rejects a configured `runtime.max_modes` below the required
count. `scenario.reference_plane_m` is an offset from `geometry.cell.height_m`
along +z into the air: `zref = height_m + reference_plane_m`. The candidate Zmax
deembedding distance is `reference_plane_m - air_height_m` (nonpositive for a
reference plane within that air span). The actual complex-S reference-plane
readback remains `not_run` and must pass real acceptance.

`prepare-cst` emits original configuration and acceptance artifacts only,
with numbered output folders and case IDs in receipts; it neither launches CST nor
claims the solver API, importer or material mapping has passed real acceptance.

`analyze` checks the full modal and independent power exports and writes
`metrics.json`, processed CSV, a standalone runnable plotting script, style and
metadata, PNG at least 300 dpi, and editable SVG. Figure style can be supplied as
`--style style.json`; `plot` accepts the same option. Optional `plot --config`
provides material measurement domains for annotations. The generated script can be
rerun in an existing compatible Python environment without this package.

Spectra CSV columns: `frequency_Hz,m,n,polarization,s_re,s_im,gamma_re_per_m,`
`gamma_im_per_m,normalization`. Normalization must be `power`; Gamma follows
`exp(-Gamma*z)`. Power CSV columns: `frequency_Hz,incident_W,reflected_W,absorbed_W,`
`transmitted_W`. Retain the original complex spectra and independent power exports.
Synthetic example files demonstrate software behavior and are not research results.

External CSV provenance defaults to `user_supplied_unverified`. Passing
`--export-origin synthetic` labels an intentionally synthetic input and records
`CST_execution=not_run`. Otherwise execution is `unverified_external`; checking
numbers cannot prove which application exported them. All analysis results remain
`screening_only` or `diagnostic_only`, with `numerically_qualified=false`.

`analyze-native` completes the offline stage after an initial `--acceptance-run`
has saved `raw-results/resulttree.json` and `model-readback.json`. Supply those
files from the same attempt and an explicit profile for their actual tree/run
IDs, labels, units and modes. It maps those saved raw values to canonical spectra
and independent power CSV, then runs the existing metrics and plotting workflow.
The profile must also bind the actual S incident column and every independent
power excitation branch to the configured TE/TM case; see the required
`excitation` fragment and supported label grammar in
[cst-validation](cst-validation.md). Missing or contradictory identity evidence
blocks mapping and analysis. Raw curves marked invalid remain evidence only.
It uses only the selected configured case and its prepared physical signature;
confirmed batches require `--case-id`. It neither infers cases from the raw data
nor constructs a backend, imports the vendor SDK, probes live resources/PIDs or
starts another solver. Mapping failure returns exit 2 and retains
`mapped-results/mapping-receipt.json`. Use a new empty output directory for each
mapping attempt, including revised profiles, so earlier evidence is preserved.

Successful saved-export analysis remains `diagnostic_only`, with
`export_origin=unverified_saved_native_export`,
`CST_execution=unverified_saved_export`, `native_acceptance=not_run` and all
qualification/physical acceptance flags false. Missing fitted-material mapping
remains an explicit `material_fit_readback` unresolved gate, even if spectra and
independent power checks pass. A profile and a saved JSON file do not certify
their actual producer or native acceptance. Optional `--style` controls the same
CSV/script/PNG/SVG exports as the ordinary analysis command.
The metrics provenance retains the mapping's
`transmission_source=read_back_PEC_boundary_assumption` and
`transmission_exported=false`: the zero transmission is a PEC boundary assumption
after model readback, and must not be interpreted as an exported native power curve.

`run` and `resume` use the shared prepared plan and runtime controller. Future live
invocations require **all** of `--backend cst`, `--authorize-live`, and
`--exclusive-resources`, plus at least one of `--acceptance-run` or
`--result-profile PATH`. Missing gates stop before backend/vendor imports,
configuration planning and output creation. An explicit result profile must map
actual result-tree leaves, run IDs, units and modes; discovery never guesses this
mapping. An acceptance run collects raw native exports for the first validation,
and unresolved material/result validation returns `failed` with
`native_validation_pending`, rather than a cached successful physical result.

The implemented experimental SDK chain creates a fresh owned
`DesignEnvironment.new()` session and `new_mws()` project, saves only the owned
project, applies original model history, checks model/mesh readbacks, starts and
polls the owned solver with bounded abort handling, saves/closes its own project,
exports the actual saved result tree, optionally maps/analyzes the exports, and
checks its environment closure. Implementation and injected-interface tests do
not establish that a CST build has accepted this chain. Native API/build,
material/geometry readback, modal S/Gamma power normalization and reference plane,
independent power closure, session lifecycle and real single-case acceptance
remain `not_run` in this release. No CLI mock worker is provided. Result receipts
are preserved; software completion never changes `numerically_qualified=false`
or `physical_certification=false`.

Use the live flags only for an explicitly authorized later invocation in a
prepared compatible CST environment with its installed Python interface/results
libraries importable. These commands are future execution examples, not package
verification steps:

```text
python scripts/absorber_cli.py run examples/single.json --out-dir outputs/native_acceptance --backend cst --authorize-live --exclusive-resources --acceptance-run
python scripts/absorber_cli.py resume examples/single.json --out-dir outputs/native_acceptance --backend cst --authorize-live --exclusive-resources --acceptance-run
```

No real CST invocation was performed while
building or testing this package. `resume` reuses the controller's integrity,
ownership and cache checks; it does not relax authorization gates.
Use `resume` for the same explicit run after a pause. A nonretryable validation
failure is retained and cannot be resimulated by increasing max_attempts or adding
a mapping profile. Use `analyze-native` on its saved raw data for offline mapping.
Explicit resume acquires the controller lock and validates input/cache hashes and
all prior owned-closure receipts before consuming the captured pause request.
A new pause arriving during validation is retained. Completed cases are skipped.
A closed cancellation acknowledged by the worker is recorded separately as
paused and does not consume the failure-attempt budget; its resumed attempt uses
a new directory. A concurrent genuine failure remains a failure, and legacy
failed receipts without cancellation evidence are not converted into pauses.
Pause reads share the controller deadline, including active-worker polling.
The controller checks again after resource and final asset checks. A request
before dispatch preserves any staged attempt as `controller_not_started`, with
`worker_dispatched=false`; this distinct evidence has no worker-cancellation
claim and does not charge the failure budget. A resumed attempt uses a new
directory. Invalid or linked pause controls are rejected by both setter and reader.

`pause` writes the runtime's pause request. `status` reads runtime evidence
and preserves backend receipts; its execution label defaults to `unknown` because a
controller receipt does not establish whether CST ran. Neither control command
launches a solver. Corrupt state is blocked, never reported complete.

Successful commands print JSON and return 0. Validation, input, capability or state
errors and failed/blocked live receipts return 2 with an explanatory message.
Argument syntax errors use standard
argparse help. Completion of offline preparation or analysis does not certify a
physical model or numerical accuracy.
