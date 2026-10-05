# Release evidence — 0.2.2-preview

**The prior 0.2.1 synthetic plate solved, but automatic native end-to-end
acceptance failed. Exact-owned manual cleanup was separately confirmed.
The 0.2.2 repair was verified offline and has not been rerun in CST.
Numerical qualification and physical certification remain false.**

## Verification

The final integrated offline command is
`python -B -m unittest discover -s tests -v`. It passed **376 tests** in 46.875 seconds.
Compilation, Draft 2020-12 schema and both example validations, basic skill
frontmatter checks and offline CLI help/validations passed. The complete
test names are delivered in the accompanying `cst-absorber-skill-0.2.2-tests.txt`.
Earlier focused runs and review probes are not added to the suite count.

Regression coverage includes held SDK object/Windows process identity,
archive inode/content drift, same-inode edits, link/alias rejection, late-save
pin updates, synchronous save failure and timeout, safe without-saving close,
startup and callback completion, cancellation, durable native supervision,
configured mesh/mesher readback, unresolved CPU/reference-plane gates,
transport provenance, exact material-leaf roles, split complex-property signs,
conductivity declarations and incomplete dense sample grids. The earlier generic
geometry/material/modal/analysis, cache integrity, pause/resume and CLI tests
remain included.

Independent reviewers reproduced held-object switching, late archive pinning and
interrupted thread startup races before their repair. Pure fake-interface and
thread tests establish software behavior; they do not establish native CST
compatibility, physical accuracy or the identity of an asynchronous archive writer.

Verified environment: Python 3.12.7, NumPy 2.3.5, Trimesh 4.11.5,
Matplotlib 3.10.8 and JSON Schema 4.26.0. No dependency was installed for this
repair. Other allowed dependency versions were not exhaustively tested.

## Native trial and current boundary

[Sanitized native findings](references/native-acceptance-0.2.2.md) record the
single original synthetic plate, exact prior failure, separately confirmed manual
cleanup and saved-result observations. Private native projects, original receipts,
logs, curve exports and process identifiers are retained locally and excluded
from this distribution.

| Component | Offline evidence in this repair | Prior real observation / remaining gate |
| --- | --- | --- |
| Geometry/material/scenario core | Generic and multi-region fixtures tested | One brick observed; imported and multiple native regions unverified |
| Held session controls and archive integrity | Drift, ownership and late-callback regressions | Prior automatic lifecycle failed; repaired native lifecycle not_run |
| Native lifetime supervisor | Worker/cleanup/query completion and interruption regressions | Real supervised shutdown of this patch not_run |
| Units, domain, lattice and mode mapping | Strict source comparisons | Prior one plate and TE/TM fundamental modes observed |
| Mesh/mesher readback | Documented getters and configured limits compared | Edge units/freshness/strict enforcement unresolved |
| CPU and reference plane | Missing/unknown evidence remains blocked | Actual solver CPU and complex-S plane/phase unresolved |
| Raw saved result export and provenance | Typed identities and unverified transports tested | Prior 40 curves / 5246 samples read; producer proof is limited |
| Material table / FD / Nth-order Fit | Exact leaves, signs and sample coverage tested | FD matched synthetic input; Fit deviated; solver linkage unresolved |
| Modal S, Gamma and power | Canonicalization and closure tested | Prior two vacuum fundamental modes observed; general normalization unresolved |

Archive drift does not disable exact held-project polling, abort or discard-close,
but save/history/solver start/export remain quarantined. Archive writer attribution
and a trusted stable snapshot are unresolved. Pathname-based SDK operations also
retain a check/use window despite identity guards. SDK caller timeouts may leave
tracked operations pending. Native CLI supervision retains the interpreter;
Python API users must explicitly create, pass and finally wait on a
`RunSupervisor`, as shown in [usage](references/usage.md).

Original table, FD interpolation and Nth-order Fit evidence are not interchangeable.
The requested `TDCompatibleMaterials=False` agrees with the documented linear
table interpolation policy, but an actual setting getter and solver-response link
remain unverified. No conclusion about any research material follows from the
synthetic case. A declared zero conductivity is not actual conductivity readback.
Unknown mesh/CPU/reference-plane/material evidence is never cleared by an empty
adapter gate list or an injected SDK.

`run`/`resume` require all explicit admission flags. Native validation-pending
receipts are retained failures and cannot become cached physical success.
`analyze-native` only maps saved evidence offline and remains diagnostic.
PEC-derived zero transmission is explicitly an assumption, not an exported
native transmission curve. Screening is not numerical certification.

## Retained synthetic demonstration

[examples/preview](examples/preview) remains the earlier synthetic demonstration,
with metrics, actual-point CSV, PNG, SVG, style/versions and standalone plotting
script. It contains one design, two regions and two materials. Frequencies are
1, 1.2 and 2 GHz; R is 0.20, 0.04 and 0, and A is 0.80, 0.96 and 1.00.
The frequency-weighted average A is 0.96; total and longest piecewise-linear
R<=0.1 widths are both 0.875 GHz under the declared gap policy. Exact zero has
a null dB value and a stated display-only marker policy.

The prior single-case skill-use trial ran offline help/validate/plan/analyze/plot
and the exported standalone script. The PNG was visually inspected and reproduced
in the same environment; SVG generation timestamps/IDs prevent byte identity.
These are synthetic software examples, not native performance measurements.

## Release boundary

`MANIFEST.json` records each distributed file's relative path, SHA-256 and size.
The archive contains only original generic code, documents, synthetic examples
and tests from explicit release categories. `.git`, private work/probes/logs,
machine paths/accounts, research data, native models/results and vendor code/help
are excluded. Ignore rules cannot remove already tracked files; each archive is
inspected separately. No open-source license is selected, extraction does not
install anything, and this delivery does not push GitHub.
