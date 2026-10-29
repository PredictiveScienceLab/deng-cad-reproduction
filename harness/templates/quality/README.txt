RELOCATED DENG QUALITY EVALUATOR

The original rubric bytes and grading tolerances are unchanged. evaluate.py and
execution.py have only project/runtime path substitutions. The runtime modules,
fixtures and relevant original unit tests retain their original logic. Historical
report-generation and campaign-qualification drivers are not included.

This directory's freeze.json is generated locally by scripts/materialize.py. It
is a distinct local evaluator identity, linked to the historical freeze by hash;
it is not a claim that the new machine/runtime has historical qualification.
Rerun affected tests after relocation or any dependency change. The full source
paper is not a runtime dependency. Its provenance hash and historical local path
remain in frozen/rubric.json solely to preserve the original rubric bytes.

Use scripts/replay.py --mode score for standard-library-only score replay from
packaged historical measurements and protected review. This verifies bindings and
recomputes all statuses; it does not newly measure CAD or independently validate
operator judgments. Use --mode full on macOS for fresh restricted source rebuild,
STEP integrity/automatic geometry checks, and reuse of the original snapshot-bound
operator measurements. Candidate source is executed only by the frozen no-fork
Seatbelt worker, never by the replay wrapper or host interpreter.

There are61 numbered criteria:19 automatic plus42 protected operator-review
criteria. Full acceptance requires all61 and all supplemental/integrity gates.
The packaged historical result has60 confirmed and S3D9 unresolved. Replay must
compute that result, not substitute a hardcoded expected score. Original rubric
and source-audit text may describe their pretrial creation state; trial execution
history belongs to results/trial-001 and is distinct from evaluator operation.
