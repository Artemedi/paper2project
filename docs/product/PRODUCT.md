# Paper2Project product contract

User scope (2026-10-31): import one project Markdown or a directory of project
memories; select research; analyze and test applicability for one or more projects;
deliver a self-contained, shareable tool. Both host-agent MCP and standalone CLI
with a user-configured model API key are required.

## Required product behaviour

1. Portable Python package, CLI and stdio MCP share one implementation. Runtime
   state lives in an explicit workspace, never beside installed code by default.
2. Import immutable project Markdown snapshots from a file/directory, preserve
   document/line/hash citations, project selection, revisions and change detection.
   Source prose is untrusted content, not runtime instructions.
3. Import research Markdown, reviewed Paper2Skill directories, or PDF/HTTPS source;
   preserve original bytes, provenance, extraction status and limitations. Extracted
   PDF text alone must not be labelled full visual review.
4. Associate research with multiple project snapshots. Persist analysis, plans,
   individual experiment outcomes and final project-specific decisions. Resume must
   detect stale evidence and never silently upgrade a text-only recommendation.
5. Hosted mode supplies context/read/search tools and accepts grounded analysis and
   experiments from an agent. Standalone mode uses a configured OpenAI-compatible
   Chat Completions endpoint; API keys are read from environment only and never
   stored/logged/exported. No vendor-specific model is assumed.
6. Source-bound code experiments pin and inventory the original code, license,
   entrypoint/dependencies, fixtures, expected-result oracle and resource limits.
   Model-generated adapters stay thin; no invented substitute scientific algorithm.
   Acquisition/build is separate from offline unprivileged container execution.
7. Run baseline/reference and candidate in separate fresh containers. A host-owned
   comparator checks actual JSON results, declared metrics and independent expected
   values. Source processes cannot write the scorer/plan/state. Persist stdout,
   stderr, exits, output hashes, timings and all failures; no PASS on skipped work.
8. Distinguish reproduction from target-project benefit. Reproduction PASS never
   proves project utility; project benefit needs a declared baseline, target data,
   independently checked metrics and quality/cost criteria. Missing author code,
   project data, dependencies or sandbox remains a visible actionable outcome.
9. Produce Markdown+JSON reports per project, portable evidence bundles and an
   installable release with usage, input examples, license notices and selftest.
   Exports omit secrets, full private memory and downloaded source by default.
10. Validate CLI/MCP equivalence, model HTTP contract, multi-project grounding,
    stale/tampered evidence rejection, sandbox isolation, actual author-code run,
    independent outcome checks, clean install and ZIP relocation.

## Non-goals and limits

No DSH Runtime/Host changes, automatic global registration or public publishing.
A language model and (for executable checks) Linux rootless Podman are explicit
external requirements, not hidden services. No guarantee that every paper has code
or every project description contains enough data for a real benefit experiment.
The existing qualified DRAG package and validated KVSET conversion remain usable.
