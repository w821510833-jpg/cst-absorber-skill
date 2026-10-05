# CST absorber skill — 0.2.3-preview

Reusable tools for one periodic PEC-backed absorber or an explicitly confirmed
batch. Geometry is not restricted to TPMS; isotropic frequency tables are not
restricted to any named material. One case can contain several regions/materials.

**0.2.2 native retest: failed.** The original synthetic flat plate reached solver
`SUCCESS` and its owned session closed automatically. The requested 1 mm sizing
produced a reported maximum edge of 1.16412 mm under the project-unit/final-mesh
assumption. The legacy 1 mm measured acceptance ceiling was not met. An archive
identity change was quarantined and blocked the automatic save/export path.
Saved raw results were read separately after closure and retained privately;
that read did not repair the failed receipt or establish archive writer identity.
The earlier 0.2.1 failure and manual cleanup remain historical observations.

**0.2.3 native trial: failed measured-mesh acceptance; automatic save, export and
owned closure recovered.** One unchanged synthetic case used one synchronous
solver dispatch and reached `SUCCESS`. The failure path automatically retained
40 raw curves / 5246 samples, with no export errors or archive quarantine. The raw
maximum edge was again 1.16412; the unchanged 1 mm ceiling failed under the
project-mm/final-mesh assumption. Getter units and selected mesh/run identity
remain unverified. Numerical qualification and physical certification are false.
The tested archive is identified in [0.2.3 findings](references/native-acceptance-0.2.3.md).
This publication updates documentation and the distribution manifest only;
all executable code and tests remain byte-for-byte at the tested revision.
No additional CST run was performed for this documentation update. Existing
executable capability/history labels have not been revised; use the dated native
findings for this historical trial, without accepting any future invocation.

`start_solver` returns asynchronously. The separate acceptance path dispatches
`Model3D.run_solver` once and waits for its documented solver/post-processing
completion, then checks actual running state and solver info, explicitly saves
results on the held owned project, and prepares matching stable file snapshots.
Only caller acceptance of the completed request can commit its new archive pin.
Existing quarantine is sticky; this path does not reclassify an old quarantined
archive as successful. The retained asynchronous path keeps its strict archive
guards and supports polling/interruption when no SDK request is pending.

This is an **explicit SDK operation trust boundary**, with exact object, process,
path, deadline and snapshot checks. It does not prove who wrote the bytes, reject
every stable competing write inside the permitted operation, or guarantee durable
filesystem completion. Outside-window drift, aliases, wrong ownership, failed
completion and late/cancelled requests remain rejected. A pending synchronous SDK
call can outlive its caller deadline; supervision retains that lifetime and does
not issue a concurrent abort or cleanup mutation.

Tetrahedral size controls are nominal sizing inputs, not a guaranteed longest
edge bound. `mesh.target_edge_m` declares the target; optional
`mesh.acceptance_max_edge_m` declares a separate measured ceiling. Legacy
`mesh.max_edge_m` retains both meanings and its previous strict acceptance value.
No tolerance factor, higher ceiling, refinement or extra solve is added
automatically. Reports retain overshoot before rejection; unknown getter units
and solve/mesh freshness still block native qualification.

Formal `Material.GetSigma` and `Material.GetRho` read actual shape-assigned material
parameters. Native conductivity must remain zero on every axis because the core
includes declared conductivity in epsilon once. A density comparison uses each
declared material density; omission does not authorize a default. The inspected
documentation supplies setters, but no verified getters for the actual FD policy
or Floquet reference plane. Those values remain unknown and fail closed. Original
Data, FD and Nth-order Fit leaves retain distinct identities and actual sample
grids. Official GUI help documents linear interpolation of tabulated properties
with “Fit as in Time Domain” off; its correspondence to the requested setter is
semantic inference, not actual readback.
Invocation/file hash association does not authenticate a native curve's solver
run or prove which material response the solver used. The full-lifecycle CPU peak
remains unverified. A subsequent read-only catalog returned only run/pass ID 0:
all 40 leaves use the Current slot with empty parameter combinations. No nonzero
archived ID was available to establish stronger native run association.

[Write-completion review](references/native-write-completion-review.md) explains
the root-cause interval, SDK contracts, mesh semantics and remaining evidence.
[Sanitized historical findings](references/native-acceptance-0.2.2.md) retain the
earlier evidence. No research-material conclusion follows from the synthetic case.
[Current evidence and minimum next steps](references/native-acceptance-0.2.3.md)
record the completed read-only catalog and the still-open qualification gates.

[SKILL.md](SKILL.md) is the agent entry point. Commands are in
[usage](references/usage.md), capability markers in
[capabilities](references/capabilities.json), and native gates in
[cst-validation](references/cst-validation.md). Two-port transmission, anisotropy,
surface impedance and finite RCS are unsupported. No implicit scan is generated.

Use an existing compatible Python environment; dependencies are listed in
`requirements.txt`. Nothing installs automatically. Extraction does not install
the skill, CST or its SDK. A later authorized native invocation needs the user's
compatible installed interface/results libraries.

```text
python scripts/absorber_cli.py validate examples/single.json
python scripts/absorber_cli.py analyze examples/single.json --spectra examples/synthetic_spectra.csv --power examples/synthetic_power.csv --export-origin synthetic --out-dir outputs/demo
python -B -m unittest discover -s tests -v
```

Live admission requires explicit authorization, exclusive resources and an
acceptance-run or actual-result mapping selection. CLI flags cannot grant
software rights or bypass tool approvals. `analyze-native` maps the same saved
raw export offline and keeps its unverified source and PEC transmission assumption.
All examples and [preview figures](examples/preview) are synthetic software
fixtures. Verification is in [EVIDENCE.md](EVIDENCE.md).

Only original generic code, documentation and synthetic tests/examples are
bundled. No vendor source/help, accounts, user materials, models or solver results
are included. Ignore rules do not remove already tracked files. No open-source
license has been selected. This candidate neither installs nor pushes GitHub.
