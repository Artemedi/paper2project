# Paper2Project 1.0.2

This version adds finding-scoped review, frozen reference attestation, explicit
run recovery, portable evidence indexing and exact threshold comparisons. ZIP
exports are published atomically. Review bindings now include the job and
snapshots. Existing unbound reviews and legacy runs remain available for audit
and may require renewed review or execution. See the product guide for details.

PDF extraction now runs in a separate Linux process with address-space, CPU,
wall-clock and output limits. Extraction still does not establish visual review.
Source and input snapshots are checked again before candidate execution; a change
blocks candidate while retaining the completed baseline evidence.
