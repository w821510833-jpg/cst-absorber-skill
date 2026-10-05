# CST native acceptance for the executable preview

The native backend is an **implemented experimental execution chain**. One
synthetic 0.2.1 case solved, but automatic end-to-end acceptance **failed**.
0.2.2 repairs are offline tested and have not been executed in CST; see
[sanitized findings](native-acceptance-0.2.2.md). Preparation is offline. Explicit authorization,
exclusive resources and acceptance-run/result-profile admission enable a later
native invocation. Development tests used injected interfaces only; they do not
establish CST build compatibility, material fitting or numerical qualification.

`prepare_cst(case, out_dir)` accepts exactly one prepared case from `build_plan`. It produces original command candidates for periodic PEC backing, explicit rectangular cell lengths, native multi-material bricks or transformed closed region STLs, dielectric/magnetic material fitting and the configured incident Floquet mode. Explicit batch cases can call the same preparation function once per listed case. No additional parameter grid or design is generated.

Before preparing artifacts, the backend rebuilds the physical signature and current geometry audit, canonical material samples and region masses from the complete case inputs. It compares them with the supplied prepared fields and rejects any mismatch. Optional CLI mode metadata is checked against independent mode enumeration. It does not silently replace altered inputs or derived data with a new plan.

`get_capabilities()` separates execution implementation from unresolved acceptance
gates. `CstBackend()` is a runtime-compatible facade `(case, run_dir, stop_event)`.
Unapproved calls return `unsupported_validation` before SDK imports or session
creation. `owned_closed=true` then means `session_created=false`, not live cleanup.
The admitted chain creates a fresh DE/project, sets units before geometry,
submits original history, checks model readback, applies CPU/mesh controls,
starts the asynchronous solver and polls it, saves/closes its own project,
exports actual saved results and closes its owned DE. Actual process observation
is limited to the PID obtained from that newly created DE; no attach, existing
session enumeration, name killing or process termination is implemented.

Solver info is retained verbatim. Exact `state == SUCCESS` is a conservative
adapter policy based on a working call pattern, not a publicly guaranteed dict
schema. Unknown/failed info stays failed; a stopped solver's project/raw results
are preserved within the remaining budget. Mesh validation failure follows the
same preservation path. It does not trigger another case or automatic retry.

SDK request timeout units are undocumented in the inspected public help, so the
adapter leaves vendor timeouts at their defaults and bounds its caller in seconds
with a daemon thread. A timed-out request may continue; another mutation is
blocked while it is pending. Verified closure requires original process absence
or observed PID reuse; access denial/unknown retains an unconfirmed state/lock.
Held-session controls and archive integrity have distinct guards. Stable inode
and SHA256 checks quarantine unaccepted archive changes before content writes
and export, while exact owned polling/abort/without-saving closure remain
available. Linked or changed run/project paths, wrong held objects and unknown
identity still block unsafe control. Explicit synchronous save is a checked
writer trust boundary; asynchronous solver replacement never silently repins.
The pathname SDK retains a TOCTOU boundary and lacks handle-based atomic-save
writer attribution. A quarantined archive blocks automatic final save/export.
Use private owned run directories. Native CLI supervision retains late worker,
cleanup and SDK obligations; API callers must explicitly use RunSupervisor.

## Preparation artifacts

- `setup.vba`, `materials.vba`, `geometry.vba`: portable original commands with `@@ARTIFACT_ROOT@@` placeholders. The implemented adapter binds the owned path and escapes VBA strings before submission. Native acceptance remains separate.
- `assets/region_*.stl`: only user-designated region meshes, transformed into mm. Source asset hashes and transformed volumes are checked against the prepared geometry audit. No STL, mesh, or material is taken from an existing CST session.
- `geometry_readback.vba`, `mode_readback.vba`: candidate readback commands. The prior synthetic brick exercised model readback; the expanded mesh getters
and other geometry paths remain unaccepted. A watertight input STL is insufficient to establish its native shape count, topology, volume or material assignment.
- `expected_geometry.json`, `expected_modes.json`, `expected_materials.csv`: expected input properties. They are not measured solver results. The expected rectangular cell is independent of the sparse sample bounding box. Native calculation-domain placement still requires readback.
- `acceptance_requirements.json`, `preparation.json`: unresolved gates, explicit `not_run` status and artifact hashes. Empty `spectra_export.schema.csv` and `power_export.schema.csv` contain headers only; they are native-output schemas, not synthetic simulated data.

## Material meaning

Prepared canonical samples use exp(+j omega t), with epsilon=epsilon'-j epsilon'' and mu=mu'-j mu''. Native table commands receive **positive loss columns** epsilon'' and mu''. The configuration sampler folds electrical conductivity into epsilon once when the original table excludes it; the native candidate sets Sigma to zero. Original conductivity and the native zero value remain in the expected-material table. Tables already containing conductivity are not given another conductivity contribution.

The native candidate uses the documented general Nth-order fitting interface.
It also requests FDSolver.TDCompatibleMaterials=False: the installed frequency
solver help documents linear interpolation of tabulated properties with TD
fitting disabled. Data list, FD - Interpolated and Fit are distinct actual
material source roles; a title containing Fit does not establish leaf role.
Requested settings and matching FD curves do not establish actual solver
linkage or native Sigma; those gates remain unresolved. Nth-order Fit is an
approximation distinct from FD table interpolation and from the core's
piecewise-linear sampling. Native fitted curves must be exported and compared
with canonical values at existing exact requested frequencies, under declared
error tolerances and coverage policy. A one-point fitting input does not establish
a valid dispersive model and needs separate native acceptance. No causality or
fit accuracy is claimed by offline preparation. Density is written through Rho
in kg/m3 when supplied; missing density is explicit rather than invented.

## Gates before any live success

1. Use a fresh `DesignEnvironment.new` and `new_mws`, save only to a new owned path, and verify the bounded solve and owned-session closure. Never attach to, discover, close or kill another user's session. Result reading must use this saved unpacked project only.
2. Read back each named region's material, solid count, native volume and bounds, and the calculation-domain origin/height. Confirm unit conversions and actual cell lengths. `UnitCellFitToBoundingBox=False` prevents sparse solids from changing the specified lattice lengths; it does not certify domain placement.
   Positive lengths use a 1 ppm relative consistency budget and positive volumes
   use 10 ppm, with no fixed absolute SI floor. Bounds also use the corresponding
   expected axis extent, so translating a thin region cannot enlarge its allowed
   size error. This is a conservative input/readback consistency policy; it does
   not establish CST CAD precision, mesher capability or a supported minimum scale.
3. Verify maximum-edge controls and readback. The executor applies tetrahedral size controls from actual domain dimensions, per-shape step width and independent solver/mesher CPU settings. It checks the configured mesher process count against the immutable runtime snapshot. Actual mesher enforcement and solver CPU enforcement remain unverified. `GetMaximumEdgeLength` is retained under an explicit project-unit assumption; its units/freshness and a strict size guarantee remain unresolved.
4. Verify the reference plane and phase. `reference_plane_m` is an air offset above `geometry.cell.height_m`: zref=height+reference_plane_m, zport=height+air_height_m. The Zmax deembedding distance is reference_plane_m-air_height_m, nonpositive for a plane within that air region. Retain native readback of the resulting plane and complex phase.
5. Export the fitted epsilon/mu response and fitting error; compare with input values. Do not substitute the input table for a native fitted response. Check measured and authorized extrapolated domains separately.
6. Read actual mode names/indexes and the saved native result tree. Confirm that every required physical mode exists at every solved point and that all planned frequencies were actually solved. A preview evaluated at one sorting frequency cannot certify whole-band mode propagation.
7. Retain actual complex S, reference impedance and Gamma with their original labels. Confirm the Gamma unit and convert it explicitly to 1/m. Gamma=alpha+j beta and an inverse-length dimension do not establish the actual export unit. Independently reconcile propagation/cutoff with the vacuum lattice; preserve native data rather than replacing it with analytic Gamma.
8. Confirm the actual Floquet S normalization is power normalization. Preserve original complex values and normalization evidence. General waveguide normalization documentation is insufficient to certify the particular native Floquet export.
9. Read independent time-averaged stimulated, outgoing, accepted and material-loss powers from the actual excitation branch. Do not assume incident power is 1 W. Reconcile total reflected modal power and independently reported reflection/absorption with incident power. For the PEC-backed profile, zero transmission must cite the verified PEC boundary. Native accepted power must not simply be renamed as independently measured material absorption.

If any required curve, frequency, mode, unit, mapping, fit or ownership proof is unavailable, stop with a specific unsupported/failed validation state. Do not fabricate spectra, use synthetic fixtures as native receipts, silently interpolate missing frequencies or mark a model completed. Even after native acceptance, this workflow provides screening/diagnostic results rather than numerical certification.

## Actual result exports and mapping

Raw `resulttree.json` and per-curve CSV retain discovered tree paths, each leaf's
actual run IDs, scalar/1D data, original labels/title, parameters, complex values
and reference impedances. Nonfinite values are explicitly tagged; partial errors
are retained. For 1D curves, conflicting `get_data()` and x/y getter readings
retain both actual numeric readings, with validity/error fields in JSON and CSV.
For 0D values, the getter counterpart and conflict details remain in JSON;
the scalar CSV does not duplicate both readings. Unequal 1D lengths
remain unequal; missing cells are empty rather than fabricated values. These
curves are marked ineligible for analysis, and canonical mapping rejects them.
Reads exceeding a point budget retain bounded actual prefixes with explicit
truncation/invalid flags; they do not claim a complete export. A declared
over-budget curve is rejected before reading. Unknown reference-impedance shapes
are retained as invalid evidence rather than broadcast into per-point values.
Only the saved owned unpacked project is read with
`ProjectFile(..., allow_interactive=False)` after closing that project.

The optional `cst-result-profile/1` requires `confirmed=true`, the exact case
signature, integer run ID, expected parameters, power normalization and its
source declaration. Selectors match exact treepath/run/title/xlabel/ylabel and
verified unit conversion. Modes additionally match actual Zmax index/name,
`m`, `n` and polarization from model readback. Powers map stimulated, independently
reflected and all declared material-loss curves; Accepted or port absorption is
rejected as a substitute for material loss. PEC transmission zero cites readback
of that boundary and remains an assumption rather than an exported T curve.

Receiving-mode identity alone is insufficient. `excitation.incident_mode` must
match the model's actual open-port fundamental (`m=n=0`) and the configured
TE/TM incidence. Native indexes are read back; the adapter never assumes TE=1 or
TM=2. Every selected S curve must identify both the receiving row and incident
column in its actual title or tree leaf. Stimulated, reflected and each
material-loss curve must share that incident port/index and the same complete
actual excitation branch. Contradictory recognizable titles and tree leaves
also fail. A caller declaration or matching scalar powers cannot replace this
evidence.

The required profile fragment below is **synthetic syntax only**; copy actual
mode identities and literal parents from the saved model and raw export:

```json
"excitation": {
  "incident_mode": {
    "port": "Zmax", "native_mode_index": 7, "mode_name": "TE(0,0)",
    "m": 0, "n": 0, "polarization": "TE"
  },
  "s_identity": {
    "field": "title",
    "template": "S{receive_port}({receive_mode}),{incident_port}({incident_mode})"
  },
  "power_identity": {
    "field": "treepath",
    "template": "actual/power/Excitation [{incident_port}({incident_mode})]"
  }
}
```

Supported S grammar is exactly the complete atom shown above: either a whole
title, or a tree leaf below explicit literal parents. Power grammar is exactly
one complete `Excitation [...]` branch below explicit literal parents, followed
by the actual quantity path. Slots occupy whole port/index tokens; arbitrary
formatting, annotations, repeated/fixed competing identities and unknown native
grammars are unsupported. This limited parser is not a universal CST label
decoder. Missing incident identity, unparseable S columns or unparseable power
branches retain respectively `incident_excitation_identity`,
`s_matrix_incident_column` or `power_excitation_identity` unresolved gates.
Wrong actual identities fail validation. Both cases preserve the receipt and
emit no canonical spectra/power CSV or labeled analysis. Successful identity
checks retain actual fields, labels, templates and captured indexes in
`mapping-receipt.json`; native/source acceptance still remains separate.

Older snapshots lacking the new per-curve validity fields are conservatively
rejected. A legacy profile also needs the explicit actual excitation evidence.
Where the saved unpacked project remains available, an authorized read-only
`export_raw_results` call can re-export that project with the current reader;
no new solve is required. Adding validity flags by hand is not a verification.

Optional material mappings distinguish `fitted_response`/`nth_order_fit` from
`fd_interpolated_response`/`fd_interpolated`. Native `real_positive_loss`
components require exact leaf/run/axis identity, `exp(+jωt)`, an explicit zero
native Sigma declaration and matching real/loss grids. The declaration does not
verify actual native Sigma. Dense curves use `exact_planned_subset` only;
missing requested frequencies fail with `material_sample_coverage`, while
observed-point errors are retained. Fit matches produce
`profile_declared_fit_matches_samples`; FD matches produce
`profile_declared_fd_interpolation_matches_samples`. Both remain diagnostic.
An FD match does not close `material_fit_readback`,
`material_solver_response_linkage` or `material_conductivity_readback`.
The source role/normalization declarations still require native verification.
Current reference-plane and mesh unit/freshness gates keep even mapped runs
validation-pending. `--acceptance-run` always returns failed/pending while
retaining actual evidence; it is not a physical-success cache entry.

## Documented API evidence

The following are relative paths within the installed **public CST 2025 Online Help**, provided as interface references rather than copied vendor text:

| Interface | Public help source |
| --- | --- |
| New environment/project, history and asynchronous solving | `Python/source/cst.interface.html` |
| Saved-project result tree, complex data, labels | `Python/source/cst.results.html` |
| Material Epsilon, Mu, Sigma, Rho and numeric dielectric/magnetic fitting | `mergedProjects/VBA_3D/special_vbalayer/special_vbalayerolayer_object.htm` (page title: Material Object) |
| Engineering loss sign and conductivity loss | `mergedProjects/3D/special_overview/special_overview_material_overview_hf.htm` |
| Frequency-domain table interpolation with Fit as in Time Domain disabled | `mergedProjects/3D/special_solvopt/special_solvopt_w3d_specials.htm` (Materials frame) |
| Brick and explicit STL unit import | `mergedProjects/VBA_3D/common_vbabasicsolids/common_vbabrick_object.htm`; `mergedProjects/VBA_3D/common_vbaimpexp/common_vbaie_stl.htm` |
| Region material assignment and solid queries | `mergedProjects/VBA_3D/common_vbasolido/common_vbasolido_solid_object.htm` |
| Explicit lattice and scan-angle conventions | `mergedProjects/VBA_3D/special_vbasolver/special_vbasolver_boundary_object.htm` |
| Background padding and component creation | `mergedProjects/VBA_3D/special_vbasolver/special_vbasolver_background_object.htm`; `mergedProjects/VBA_3D/special_vbalayer/special_vbalayerocomponent_object.htm` |
| Floquet modes and reference-plane distance setter | `mergedProjects/VBA_3D/special_vbaports/floquetport_object.htm` |
| Mesh type/count/edge and configured mesher controls | `mergedProjects/VBA_3D/special_vbamesh/special_vbamesho.htm` |
| Single frequency samples and native power-loss calculation | `mergedProjects/VBA_3D/special_vbasolver/special_vbasolver_fdsolver_object.htm` |
| Global solver frequency range | `mergedProjects/VBA_3D/special_vbasolver/special_vbasolver_solver_object.htm` |
| Gamma definition; time-averaged power meaning | `mergedProjects/3D/special_postpr/special_postpr_pp_modeplot.htm`; `mergedProjects/3D/special_postpr/special_postpr_power_view.htm` |

No vendor SDK code or installed help content is bundled. Documented interfaces and locally rendered command candidates remain separate from an executed native acceptance record.
