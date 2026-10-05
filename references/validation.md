# Evidence levels and acceptance

The preview validates software contracts using synthetic data. It has not run
CST, and cannot certify a physical design. `screening_only` is a file-analysis
result, including when independent CSV power closure passes. Mock controller
workers prove orchestration behavior, not material import or solver accuracy.

The supported offline profile is a rectangular XY periodic cell in vacuum with
a PEC backing boundary, one incident fundamental TE or TM mode, azimuth zero,
and theta from 0 to 45 degrees. Frequencies and material regions belong to that
single design. A confirmed explicit list is required for multiple designs.
`reference_plane_m` is an upward air offset from `geometry.cell.height_m`.
The Zmax port is at height plus `air_height_m`; candidate deembedding distance
is `reference_plane_m - air_height_m`. The actual reference plane and complex
phase remain real readback/acceptance items.
No fixed period cutoff is imposed: independent lattice enumeration determines
the required propagating/cutoff mode union. A configured mode limit below that
union is rejected, never silently truncated. This does not establish converged
evanescent-mode or mesh resolution.

STL validation checks units, transforms, watertightness, winding, one connected
shell, positive volume and cell bounds. It cannot establish generic triangle
self-intersection or disjointness when an STL bounding box intersects another
region. Those cases retain `unverified` audit fields. Bricks with positive volume
overlap are rejected. Multi-region mapping in CST remains a real acceptance item.

Material tables are relative dimensionless epsilon and mu. All geometry and
analysis frequencies use SI; only table frequency columns carry a declared unit.
Canonical values use exp(+jωt) and negative imaginary passive loss. Conductivity
is folded once into epsilon unless the input states it is already included;
native candidate conductivity is zero. Density is per material in kg/m3; absent
density yields unavailable mass. Sampling/interpolation is not a causal fit.
Every point must be covered by epsilon and mu measurement domains or explicit
linear-extrapolation authorization with a frequency range.

CSV spectra must declare power normalization and Gamma in 1/m under exp(-Gamma*z).
Those declarations are checked against the rectangular vacuum lattice and a
separate power CSV. The analyzer cannot prove their solver origin. Preserve the
actual exported labels, units, modal names and original power branches before
converting a real export to this CSV contract. Never guess Gamma units or assume
incident power is one watt.
The exported transmission ratio is retained without clipping. The PEC boundary
assumption is separately recorded as theoretical zero; a residual above the
declared power tolerance is rejected.

Metrics use linear reflection at actual frequencies. Average values require a
fully covered band with no adjacent gap above the declared limit. Threshold
crossings are linearly interpolated in R; report both total and longest continuous
width. The valley is the deepest sampled point, with its frequency. A shift needs
an explicitly supplied comparator and matching definitions; this preview does
not invent an additional sample or run. Exact zero has a null dB value and a
separate flag, with a labeled display floor only in the figure.

The controller tests explicit case order, immutable content-bound snapshots,
cache artifact hashes, pause/resume, closure blocking, bounded wall time and
resource probes, and PID reuse/foreign identity rejection. CPU/mode settings are
worker obligations; the experimental native worker applies documented solver and
mesher settings, but real enforcement has not been tested. Offline tests inject
identity callbacks and vendor surfaces. The authorized native transport observes
only its newly created PID and uses the complete ownership comparison before SDK
mutations; it never enumerates or terminates existing processes.
An unconfirmed thread/process closure retains a durable lock. The controller's
short confirmation wait is conservative blocking, not a universal solver-close
budget. Choose run budgets from actual comparable history and user efficiency
targets; no fixed 15-minute failure rule is supplied.
Artifact/snapshot hashing and resume validation share that wall deadline.
Timed-out read-only I/O cannot publish a late receipt or upgrade completion;
the controller records a durable blocked marker that prevents automatic reuse.
When the worker has already ended and its owned closure is confirmed, that
read-only timeout needs no process lock; an active or unconfirmed worker retains
the lock. `status` is read-only and reports its own inspection timeout without
writing a blocked marker.

The executable first-acceptance path is separately admitted by explicit execution
authorization and exclusive resources; unresolved validation yields a failed
receipt while retaining raw evidence. Before claiming native support, complete
every item in [cst-validation.md](cst-validation.md) with build-specific evidence.
Keep raw model, spectra, material data and acceptance records outside a public
skill repository. Capability markers remain `not_run` until that evidence exists.
