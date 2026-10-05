# Candidate evidence — 0.2.3-preview

**This is an offline review candidate; it has not been run in CST. The completed
0.2.2 native retest failed end-to-end acceptance despite solver SUCCESS and
confirmed automatic owned closure. Numerical qualification and physical
certification remain false.**

## Offline verification

The final integrated command is `python -B -m unittest discover -s tests -v`.
The candidate code passed 489/489 tests in 62.058 seconds (exit 0). The manifest-only correction changes no scripts, tests or example inputs; its archive and patch checks are recorded separately.
The 0.2.2 source previously passed 376 offline tests in 46.875 seconds; that is
historical evidence for that source, not the candidate's final suite result.

Candidate regressions address the named solver/save operation window, caller-only
archive pin acceptance, late/cancelled/failed completion, strict alias/path/object
and process checks, stable snapshots, nominal mesh targets versus measured
ceilings, preservation of oversize reports, Sigma XYZ and density getter records,
immutable input association and raw archive drift. Existing geometry/material,
modal/power/analysis, cache, pause/resume, CLI and native-lifetime tests remain
required. The final test log and review report identify the actual executed tests
and any unresolved results.

Pure injected interfaces, worker threads and temporary synthetic files establish
software behavior. They cannot establish native compatibility, an archive writer,
file durability, material usage by a solver, or numerical accuracy. No dependency
was installed and no CST process, new case, material or frequency was started for
this candidate. The prior verified environment was Python 3.12.7, NumPy 2.3.5,
Trimesh 4.11.5, Matplotlib 3.10.8 and JSON Schema 4.26.0; other allowed dependency
versions were not exhaustively tested.

## Historical 0.2.2 native retest

One solve used the unchanged original synthetic flat plate. Solver info reported
SUCCESS. Automatic exact-owned closure and the retained supervisor's completion
were confirmed without manual termination. The controller/native receipt still
failed with `native_archive_integrity_failed`; it was not rewritten as successful.

A nominal 1 mm mesh request produced a raw maximum-edge getter value 1.16412.
Using the project mm conversion and final-mesh assumption gives 1.16412 mm,
16.412% above the legacy measured ceiling. Those assumptions remain unverified;
the plausible value and matching final cell count do not prove getter units or
solve/mesh freshness. The first error was the mesh comparison; subsequent failed
preservation was blocked by sticky archive identity quarantine. No old receipt
has been repaired by this candidate.

A separate read-only saved-result export after owned closure retained 40 curves
and 5246 samples privately. It was not an automatic controller export and does
not authenticate the writer of the changed archive. Actual solver-stage records
reported two CPU threads/cores; two initial mesher-stage records reported two
threads. These scoped observations are not proof of a process-wide peak cap.

Original Data, FD and Nth-order Fit were separately inspected for that synthetic
material. FD samples matched the original table diagnostically, while Fit differed
and lacked an exact requested interior sample. There was no actual Sigma/Rho
getter readback, FD policy getter, reference-plane getter or authenticated native
solver/material-curve linkage in that trial. No research-material conclusion is
supported. The earlier 0.2.1 automatic failure and separate manual cleanup remain
historical, not the status of the 0.2.2 retest.

[Write-completion review](references/native-write-completion-review.md) gives the
candidate's root-cause interval, protocol, primary documentation and test matrix.
[Earlier sanitized findings](references/native-acceptance-0.2.2.md) retain the
prior observations. Private projects, receipts, logs, curve exports and process
identifiers are excluded from this distribution.

| Component | Candidate offline evidence / design | Real observation and remaining gate |
| --- | --- | --- |
| Geometry/material/scenario core | Generic and multi-region fixtures | One synthetic brick observed; imported and multiple native regions unverified |
| Held controls and archive integrity | Named synchronous completion/save window; caller-only pin; sticky quarantine | 0.2.2 owned closure confirmed; new completion path not run; writer and durability unproved |
| Native lifetime supervisor | Pending SDK and cleanup obligations retained | 0.2.2 automatic shutdown confirmed; synchronous candidate path not run |
| Units, lattice and modes | Strict contracts and source comparisons | Two vacuum fundamental modes observed; general mode/power normalization unresolved |
| Mesh | Nominal target separated from measured ceiling; oversize report retained | 1.16412 mm under assumptions exceeded legacy 1 mm; units/freshness unverified; no enforced hard cap |
| CPU and reference plane | Scoped resource evidence retained; unknown plane stays blocked | Prior solver-stage CPU=2; global peak and complex-S plane/phase unresolved |
| Formal material parameters | Actual-shape Sigma XYZ/Rho getter generation and strict parser | Candidate getter path not run; parameter agreement does not prove solver usage |
| Material Data / FD / Fit | Exact leaves and real grids remain distinct | Prior synthetic FD matched and Fit deviated; actual FD policy and response linkage unknown |
| Solver/result association | Case/operation/file hashes and actual curve IDs retained | Same invocation is weaker than native curve-run authentication |

The documented synchronous `run_solver` waits for solver and post-processing.
A subsequent explicit held-project save and stable snapshots form an SDK operation
trust boundary; they are not a documented writer-drain, fsync or writer identity
proof. Stable competing bytes inside that permitted window cannot be distinguished
by ordinary path/hash checks. Outside-window changes and invalid bindings remain
quarantined. A pending synchronous request cannot be concurrently aborted or
closed, and caller expiry does not terminate native work immediately.

The inspected FD policy setter documentation is not evidence of a general
volumetric epsilon/mu interpolation promise. Requested policy and observed FD
curves remain distinct from an actual policy getter. Unknown unit, freshness,
CPU, plane, policy and solver-material linkage gates cannot be cleared by an empty
adapter list, matching numeric values, a configured value or an injected SDK.

Native admission remains explicit. Validation-pending/failed receipts cannot
become cached physical success. `analyze-native` is offline diagnostic mapping;
PEC-derived zero transmission is a boundary assumption. Screening does not
become numerical certification.

## Retained synthetic demonstration

[examples/preview](examples/preview) remains the earlier software demonstration
with metrics, actual-point CSV, PNG, SVG, style/versions and standalone script.
It contains one design, two regions and two materials. At 1, 1.2 and 2 GHz,
R is 0.20, 0.04 and 0, and A is 0.80, 0.96 and 1.00. The frequency-weighted
average A is 0.96; total and longest piecewise-linear R<=0.1 widths are both
0.875 GHz under the declared gap policy. Exact zero has null dB and a display-only
marker policy. These figures are synthetic examples, not native measurements.

## Package boundary

`MANIFEST.json` is a repository file and is included in the full ZIP. It records
all 61 other distribution files using safe relative paths, SHA-256 and byte sizes.
The manifest explicitly excludes itself (`excluded_paths=["MANIFEST.json"]`);
there are 62 files in the full ZIP. Its own hash and size are recorded in the
separate review-package source record. This avoids a recursive self-hash.

Bundles contain only original generic code, documents and synthetic examples/tests
from explicit categories. Private work, real native logs, machine/account paths,
research data, native models/results and vendor code/help are excluded. Every
archive is inspected; ignore rules do not remove tracked files. No open-source
license is selected. This correction does not install or push GitHub.

The local-base patch targets `ca33b46`, where the manifest was absent. A separate
remote-base patch targets `a938754e1bd28b794844e6af85565b86659c6b33` and updates its
existing 0.2.2 manifest rather than adding a conflicting second file. This round
read the immutable GitHub commit/tree/manifest without writing GitHub; all 53
remote blobs matched the historical 0.2.2 archive. Both patches are applied to
their exact baseline copies, then every one of the 62 resulting files is checked
against the final distribution. Neither path may leave the old 0.2.2 manifest.
