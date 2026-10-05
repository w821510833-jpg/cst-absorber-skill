# CST absorber skill — 0.2.2-preview

Reusable tools for one periodic PEC-backed absorber or an explicitly confirmed
batch. Geometry is not restricted to TPMS; isotropic frequency tables are not
restricted to any named material. One case can contain several regions/materials.

This revision implements an **experimental executable CST SDK chain**: fresh
owned project, original model history, model/mesh readback, bounded solver
start/poll/abort, saved-project result-tree export and owned closure. One synthetic
flat plate was solved with 0.2.1, but **automatic end-to-end native acceptance
failed** after an archive identity change blocked polling and closure. Exact-owned
manual cleanup and raw export were separately verified; the failed receipt was
preserved. **0.2.2 repairs are offline tested and have not been rerun in CST.**
The initial acceptance path preserves raw evidence and returns validation pending;
unresolved gates prevent caching a successful physical result. Analysis and plots
are screening/diagnostic; numerical qualification and certification are false.
`analyze-native` maps and analyzes that same saved raw export offline, without
another solve. It preserves the unverified source and PEC transmission assumption.

This patch separates held-session control from archive integrity: exact owned
polling, abort and without-saving closure remain available after archive drift;
save/history/solver dispatch/export retain identity and stable SHA256 gates.
Unexpected archive changes are quarantined, never silently rebound as solver
output. Async writer attribution and a trusted stable-snapshot path remain
unresolved, so quarantine still blocks automatic final save/export. Native CLI
supervision retains pending worker/cleanup/SDK obligations after the case work
deadline; unknown ownership may require manual intervention. Source roles for
original tables, FD interpolation and Nth-order Fit are kept separate.

Save workers prepare a snapshot; the caller commits its archive pin only after
accepting the completed request. Abandonment and commit share the same lock.
Requests remain reserved until actual callback completion and caller settlement,
including interrupted thread startup. Timing out cannot permit a late pin commit
or a parallel replacement request.

[Sanitized native findings](references/native-acceptance-0.2.2.md) explain the
single-case evidence, synthetic Fit deviation, offline repairs and remaining
reference-plane, phase, mesh-unit/freshness, actual CPU and material-linkage
gates. No research-material conclusion or numerical certification is implied.

[SKILL.md](SKILL.md) is the agent entry point. Commands are in
[usage](references/usage.md), implementation/acceptance markers in
[capabilities](references/capabilities.json), and remaining native gates in
[cst-validation](references/cst-validation.md). Two-port transmission, anisotropy,
surface impedance and finite RCS are unsupported. No implicit scan is generated.

Use an existing compatible Python environment; dependencies are listed in
`requirements.txt`. Nothing installs automatically. Later personal installation
can copy the whole skill directory to the location recognized by your agent.
Extraction does not install the skill, CST or its SDK. A later authorized native
invocation also needs the user's compatible installed interface/results libraries.

```text
python scripts/absorber_cli.py validate examples/single.json
python scripts/absorber_cli.py analyze examples/single.json --spectra examples/synthetic_spectra.csv --power examples/synthetic_power.csv --export-origin synthetic --out-dir outputs/demo
python -B -m unittest discover -s tests -v
```

Live command admission requires explicit authorization, exclusive resources and
an acceptance-run or actual-result mapping selection. See usage before invoking
it; CLI flags cannot grant software rights or bypass tool approvals. All examples
and [preview figures](examples/preview) are synthetic software fixtures, with no
physical performance claim. Verification is in [EVIDENCE.md](EVIDENCE.md).

Only original generic code, documentation and synthetic tests/examples are
bundled. `.gitignore` protects default private input/output locations and native
assets; it does not remove files already tracked. No vendor source/help, accounts,
user materials, models or solver results are included. No open-source license
has been selected.
