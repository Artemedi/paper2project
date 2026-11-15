# Paper2Project 1.0.1

This release improves the reliability of local research experiments and makes exported
evidence easier to share after inspection.

- The Linux runner now handles a short process-exit race during output inspection. Permission
  errors for live processes still fail the run.
- Portable exports replace known host paths with evidence references. Manifest schema 2
  distinguishes the exported projection from the original workspace objects. Inspect exports
  before sharing, especially when including memory or arbitrary text.
- Quality and cost guards can compare baseline and candidate measurements directly.
  Reproduction status and measured benefit remain separate.
- An analysis can bind a claim to an individual citation. The report surfaces advisory warnings
  about possible negation, condition and unit mismatches. The existing exact citation checks
  and independent review requirements still apply.

## Qualification

The Linux working-tree build passed 160 tests and 14 qualification stages on Bazzite with
rootless Podman enabled. An installed build passed nine fresh paired runs across three lab
workloads. A subsequent two-project pilot verified both outcomes of the benefit workflow:
a scoped positive result after independent review and a negative result when a required
no-regression guard failed. The results apply to the declared data and checks.

The package requires Python 3.11 or later. For installation and experiment requirements, see
the [user guide](product/README.md).
