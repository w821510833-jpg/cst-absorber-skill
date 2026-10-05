# CST absorber skill — 0.2.1-preview

Reusable tools for one periodic PEC-backed absorber or an explicitly confirmed
batch. Geometry is not restricted to TPMS; isotropic frequency tables are not
restricted to any named material. One case can contain several regions/materials.

This revision implements an **experimental executable CST SDK chain**: fresh
owned project, original model history, model/mesh readback, bounded solver
start/poll/abort, saved-project result-tree export and owned closure. Tests inject
SDK/result interfaces. **Real CST execution and native acceptance remain not_run.**
The initial acceptance path preserves raw evidence and returns validation pending;
unresolved gates prevent caching a successful physical result. Analysis and plots
are screening/diagnostic; numerical qualification and certification are false.
`analyze-native` maps and analyzes that same saved raw export offline, without
another solve. It preserves the unverified source and PEC transmission assumption.

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
