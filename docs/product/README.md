# Paper2Project

Import your project memories, import a research source, and get an evidence-backed answer to
"does this research apply to my project(s)?". Every step is stored in a local workspace, every
claim cites an exact snapshot line range, and a text-only opinion is never reported as a verified benefit.

Two ways to drive it, one implementation:

- **Host-agent mode**: `paper2project serve` is a stdio MCP server; your agent (Claude Code, etc.) does the reasoning.
- **Standalone mode**: `paper2project analyze` asks an OpenAI-compatible Chat Completions endpoint you configure.

Requirements: Python 3.11+. Optional: `pypdf` (`pip install '.[pdf]'`) to import PDFs; rootless Podman and a
local container image to run experiments. Linux x86_64 is the tested platform. Native Windows
workspace commands require a Unix filesystem lock and are unsupported. Run under Linux in WSL
with the workspace on the Linux filesystem if using a Windows machine. WSL execution has not been qualified.

## Install and check

```bash
unzip paper2project-1.0.2.zip && cd paper2project-1.0.2
python3 -m venv .venv && .venv/bin/pip install .          # or: PYTHONPATH=src python3 -m paper2project ...
.venv/bin/paper2project selftest                           # offline, no container, no model
.venv/bin/paper2project doctor                             # shows container runtime / PDF support
```

State lives in a workspace directory (`--workspace`, `$PAPER2PROJECT_WORKSPACE`, or
`~/.local/share/paper2project`), never beside the installed code.

## Workflow

```bash
paper2project project import ./memories                    # directory: one project per subdirectory/.md/.txt file
paper2project project import ./CLAUDE.md --name "My app"  # single Markdown file
paper2project research import ./paper.pdf --title "Paper"  # .md/.txt/.pdf/https URL/reviewed package directory
paper2project job create --research paper --project my-app --project other-app
paper2project job context JOB                              # line-numbered, citable chunks (untrusted data)
paper2project analyze JOB --base-url https://HOST/v1 --model NAME --yes-send   # standalone mode
#   or: paper2project submit-analysis JOB analysis.json --author "analyst"
paper2project experiment add JOB plan.json
paper2project experiment run JOB EXPERIMENT
paper2project review-packet JOB --project my-app
paper2project review JOB --project my-app --decision pass --notes "..." --reviewer "reviewer" --analysis ANALYSIS_HASH --evidence EVIDENCE_HASH
paper2project report JOB                                   # Markdown (or --format json)
paper2project export JOB out.zip                           # report + manifest; --include-memory adds private text
```

Re-importing a changed project or research source creates a new revision. Jobs stay bound to the
snapshots they were created from, are reported as **STALE** once the current memory differs, and refuse
to run experiments until you create a new job.

### Analysis format

One assessment per selected project. Each cites at least one passage of the research and one of that
project (`document_id` from `job context`, the exact line range and a verbatim `quote` of 8–2,000 characters
across at most 12 lines). PDF page headings inserted by the extractor and citations to other projects
are refused. `relevance` is `candidate`, `not_applicable` or `needs_evidence`. An applicability
rejection needs a current project review. A submitted rejection still permits registering a test.
Citations are checked mechanically. Use the adjacent source lines and an independent review to assess support.

### Scoped findings (experimental)

When several ideas apply to different components of one project, submit `finding_schema: 1`
and a `findings` array in the assessment instead of a project-level `relevance` label.
Keep the assessment rationale and research/project citations. Each finding needs a unique
`id`, `target`, `claim`, `premise`, `falsifier`, `proposed` and `research_citations`.
The proposal can be `candidate`, `not_applicable`, `already_implemented` or `needs_evidence`.

Attach the minimal readable project artifact before creating the job. A finding's
`project_artifacts` references include `name`, `sha256`, `start`, `end` and an exact `quote`.
Without a matching attachment, omit the reference and name the evidence gap in
`missing_evidence`. The app validates the declared attachment and span against the job's
snapshot. It reports `NEEDS_EVIDENCE` or `PENDING_SEMANTIC_REVIEW` per finding in both formats.
Use those per-finding values when filtering the report.

The review packet retains the local attachment anchor. A portable export of that reference
retains its hash and span and omits its name and quote. The export includes current scoped
judgments and their bindings in `finding-reviews.json` when available. The projection omits
reviewer names, notes, independence rationale, and attachment names and quotes. Inspect any
other authored text before sharing.

Use `submit-analysis --author` to record the analyst. A separate reviewer reads the
`paper2project review-packet JOB --project PROJECT --blind` packet and runs
`paper2project review-finding JOB review.json --project PROJECT`.
The JSON names `finding_id`, its `evidence` token from `finding_evidence`, the `reviewer`,
`notes`, the `premise` judgment (`supported`, `contradicted` or `unknown`) and the
`test_link` judgment (`relevant`, `irrelevant` or `unknown`). For a definite judgment
include the `experiment_id` from the test binding. Each experiment can have its own
judgment. The binding includes the plan and the frozen source and input trees. Use
`unknown` for a review limited to the premise. To reach a reviewed
disposition, include `second_observation` using the same anchor fields as an artifact
reference but pointing to a different attached artifact and a different quote. Explain in
`independence_basis` how that observation can disagree with the original evidence.

The app records whether the reviewer supported applicability, contradicted the premise,
or rejected the test link. Its checks bind the assessment and both evidence anchors.
Reviewers judge whether the observations are substantively independent and relevant.
Declared names establish attribution only. A finding's test-link review establishes
whether the test measures the finding. A scoped experiment also needs passing measured
criteria and a benefit review with a frozen reference to support demonstrated benefit.

Give the reviewer only the blind packet at the first stage. It shows source context,
the claims, their premises, exact project anchors, declared data and comparison, and planned
metric names. It omits the author's proposals, expected-value fields and run outcomes.
Keep expected outcomes and run results out of the free-text provenance. It controls presentation only.
Someone with the full workspace can read the complete report.

For an experiment on a scoped analysis, set `finding_id` and provide
`provenance: {oracle, data, comparison}`. The oracle is an exact anchor in a frozen
project attachment. Describe the input scope and the difference between the two
approaches in `data` and `comparison`. Register plans before the reviewer signs a
finding. Adding a bound plan invalidates that finding's earlier review. The report
lists which attempts belong to each finding and the oracle digest. Human review
judges whether the oracle and input scope support the measurements.

### Importing a qualified Paper2Agent package

From the adapter checkout, run `PYTHONPATH=src python3 tools/p2a.py handoff PAPER_ID --workspace WORKSPACE`.
The adapter checks current qualification, then imports the verified text and binds the original
source hashes, review fingerprint, coverage and limitations to the product snapshot. The research
list, job context and report expose the coverage. Portable reports carry the qualification
metadata. The qualification includes the exact review and manifest digests and resolves
the origin from the reviewed source manifest. The adapter captures qualification metadata
once and the product checks it against the imported bytes before recording coverage.
Original source files stay in the local store. Older handoffs without those digests display
`PAPER2AGENT_LEGACY_REVIEW_UNVERIFIED`. Repeat the handoff against current qualified
evidence to restore verified coverage. Generic imports retain their extraction
or unattested coverage status.

### Standalone model mode

`--base-url` and `--model` are required (nothing is assumed). The API key is read from the environment
variable named by `--key-env` (default `PAPER2PROJECT_API_KEY`); it is never stored, logged or exported.
The command sends project and research text to that endpoint, so it refuses to run without `--yes-send`.
Rejected analyses are returned to the model with the validation error (3 attempts). If the local context budget
would omit chunks, standalone analysis stops before contacting the model. Read the missing material with MCP
`document_read` or prepare and submit an analysis locally.

### Host-agent (MCP) mode

```json
{"mcpServers": {"paper2project": {"command": "/ABS/PATH/.venv/bin/paper2project",
  "args": ["--workspace", "/ABS/PATH/workspace", "serve", "--allow-root", "/ABS/PATH/memories"]}}}
```

Imports are limited to `--allow-root` directories (none given: all local imports denied). URL imports need
`--allow-network`; container experiments need `--allow-execution`; `analysis_review` needs `--allow-review`, which is off by
default so an agent confined to MCP cannot approve itself (use `paper2project review` from a separate session or person). This is
a gate on the MCP channel only: an agent with shell access to the same workspace can still run the CLI and name any reviewer, so the
review records a declared human or separate-session identity. Keep analysis and review permissions separate.
An explicitly matching analyst and reviewer name is refused. Tools: `projects`, `research`, `project_import`,
`research_import`, `job_create`, `job_status`, `job_context`, `document_read`, `analysis_submit`, `review_packet`, `analysis_review`,
`experiment_add`, `experiment_run`, `experiments`, `report`. This project does not edit any client configuration.

## Experiments: reproduction versus project benefit

An experiment runs the **original** author code, pinned to a 40-hex revision with its license recorded, as two
arms (`baseline`, `candidate`) in two separate fresh containers: offline, non-root, read-only root filesystem,
all capabilities dropped, no-new-privileges, CPU/RAM/PID/time limits, source and inputs mounted read-only, and
only an empty `/output` (plus a `noexec` `/tmp` tmpfs) writable. SELinux confinement stays on: the source,
input and output mounts are workspace-owned copies and are relabelled privately (`Z`), so the container keeps its SELinux type
and can only touch its own labelled mounts (checked with real Podman on an Enforcing Fedora host). The plan, scorer and workspace are never mounted. Each arm must write
`/output/result.json`. The host then compares actual JSON values with expected values **you declared before
the run from an independent source** (hand calculation, published table, other implementation).

```json
{"kind": "project_benefit", "project_id": "my-app", "image": "localhost/my-image:tag",
 "source": {"path": "/abs/author/checkout", "revision": "<40 hex>", "license": "MIT"},
 "input": {"path": "/abs/inputs"},
 "arms": {"baseline": ["python3", "tool.py", "/input/a.jsonl"], "candidate": ["python3", "tool.py", "/input/b.jsonl"]},
 "metrics": [{"name": "hits", "path": "results.0.page_hits", "comparison": "exact",
              "expected": {"baseline": 0, "candidate": 2}}],
 "benefit": {"metric": "hits", "direction": "increase", "min_delta": 2,
             "guards": [{"metric": "cost", "op": "le", "value": 10}]},
 "limits": {"timeout": 120, "memory_mb": 1024, "cpus": 2, "pids": 128}}
```

- `kind: reproduction` checks only that declared values are reproduced. It can never yield a benefit.
- `kind: project_benefit` additionally needs an `input` directory with the target data, distinct baseline/candidate
  commands, numeric independently expected values for both arms, `min_delta > 0`, a benefit-metric tolerance below half of
  `min_delta`, and at least one quality/cost guard on a different metric. Every `project_benefit` experiment of a project must
  meet its criteria for a benefit claim.
  A passing project review without reference verification records `BENEFIT_CRITERIA_MET`.
  To support `BENEFIT_DEMONSTRATED`, attach the independent reference as project evidence and
  create the job against that snapshot. Name the attachment in the plan's `reference` field.
  The JSON attachment contains `schema: 1`, a `method` describing how the values were
  obtained, `source_tree` and `input_tree` SHA-256 digests, and `expected` keyed by metric
  name with the exact per-arm values from the plan. Use `paper2project.execution.tree_hash(path)["sha256"]`
  to compute tree digests. A declared Git revision must match the source's HEAD. Standalone checkouts and
  standard linked Git worktrees are supported. A linked checkout's administrative
  directory must be within the allowed roots and point back to that checkout; its
  common Git directory must also be allowed. Redirected or inaccessible metadata
  cannot support reference attestation.
  A separate reviewer checks the intervention, target data and whether the values
  came from an independent reference. In CLI review use `--verify-experiment EXP_ID`
  for each verified benefit experiment, together with the current analysis and evidence
  hashes from `review-packet`. Names record declared attribution. Adding a plan or
  run requires a current review. In multi-project jobs, review each project separately.
- Git metadata in a standalone checkout or an allowed linked worktree is compared with the declared revision without running Git. If the metadata cannot be verified, the revision is recorded as unverified (`head_matches: false`). `head_matches: true` only
  means that `.git/HEAD` names the declared commit; it does not prove the working tree is clean or matches that commit.
  Symlinks and special files (FIFO, socket) in a source or input tree are refused. The whole tree, including `.git` and file modes, is hashed.
- Missing image, runtime, changed source/inputs, non-zero exits, missing `result.json` and failed checks are
  recorded as BLOCKED/FAIL with stdout, stderr, exit codes, timings and output hashes. Nothing skipped passes.
- Source or input changes after a run turn that evidence STALE.

Project ids come from file or directory names; a name with letters outside a-z gets a short hash suffix. Names that differ only in case or punctuation (`My App`, `my-app`) share an id: re-importing one under the other is refused unless you pass an explicit project or research id.

Project decisions in the report: `APPLICABILITY_PENDING_REVIEW`, `ANALYSIS_REVIEW_REQUIRED`,
`NOT_APPLICABLE`, `ANALYSIS_ONLY`, `EXPERIMENT_NOT_RUN`, `EXPERIMENT_BLOCKED`,
`EXPERIMENT_FAILED`, `EXPERIMENT_INCOMPLETE`, `NEEDS_PROJECT_EVIDENCE`,
`REPRODUCED_BENEFIT_UNPROVEN`, `BENEFIT_PENDING_REVIEW`, `BENEFIT_CRITERIA_MET`, `BENEFIT_DEMONSTRATED`,
`MIXED_EVIDENCE` (some experiments passed, others did not), `STALE`, `STALE_EVIDENCE`. Verdicts are re-derived from the
stored plan and result objects on every report. Per-arm exit codes, errors, timeouts and result hashes
come from a sealed content-addressed record. The report evaluates all stored attempts in insertion
order. A later pass retains earlier failures in the project verdict, and changes to a displayed
timestamp cannot change the attempt order. Someone with write access to the workspace database and
object store can still change the evidence. A declared revision without Git metadata and an image
that cannot be checked are flagged in the report.
A passing run proves the stated numbers on the stated inputs, not production benefit: real target data,
a declared baseline and quality/cost criteria are required for that.

Dependencies are your job: build the image with network access first, then experiments run with none. This
tool never executes source on the host and never invents a substitute algorithm. `examples/kvset-demo` runs
a real author tool on synthetic traces (see its header comment; the synthetic benefit is a pipeline demo only).

## Operating mode (limited, owner-defined 2026-11-15)

1. Run directories and `/output` live on a separate filesystem of limited capacity that holds no critically important data.
2. Linux with rootless Podman in the tested configuration: Fedora/Bazzite with SELinux enforcing.
3. The code that runs and its dependencies come from trusted sources and are checked before the run. Automatic execution of unchecked third-party or generated code is outside this mode.

The `/output` control reduces the chance of uncontrolled disk use but is not a hard quota. Isolating the run directory does not automatically cover the Podman storage, system journals and other host paths.

This mode does not confirm that the system is fit to execute arbitrary untrusted code safely.

## Limits

- Imported PDF text is extracted without visual review (`TEXT_EXTRACTED_NOT_VISUALLY_REVIEWED`); tables, math,
  figures and supplements need source review or a reviewed package directory.
- A language model is an explicit external requirement; analyses are only as good as the model and reviewer.
- Not a sandbox against a hostile Podman/kernel; run only code you have inspected.
- Local evidence hashes detect accidental or casual change, not an actor who can rewrite the whole workspace.
- URL import resolves the host, validates every returned address and connects to a validated address (the TLS name stays the
  host name), including after redirects; environment HTTP proxies are ignored, so there is no proxy-side resolution. Remaining:
  the check trusts the system resolver's answer; the hard-coded `198.18.0.0/15` allowance exists for TUN fake-IP DNS and applies only to arxiv.org, www.nature.com,
  github.com and raw.githubusercontent.com. Import research from sources you trust, or from local files.
- At registration the source and input trees are copied into `<workspace>/snapshots/<experiment>/`, the copy is hashed and must
  equal the hash of the live directory (otherwise registration fails: the directory changed while being copied). Containers
  mount only the copy, and its hash is checked before baseline and again before candidate. If either source or input snapshot
  changes between arms, candidate is blocked and the completed baseline evidence is retained. Later edits of the live directory neither change what
  runs nor invalidate the result: the result belongs to the snapshot hash, not to the current directory. Modes are copied
  exactly because they are part of the hash, so the copy is not made read-only on the host: a process of the same user with access
  to the (mode 0700) workspace could still change it between the pre-run check and the container start. Disk use is up to the
  tree limits (512 MiB, 20 000 entries) per experiment and arm input.
- `packed-refs` larger than 8 MiB or a `.git` ref file larger than 4 KiB leaves the revision unverified (`head_matches: false`).
- Decisions are recorded, not only displayed: `paper2project decisions JOB` (and every `export`) stores one immutable,
  content-addressed record per project (decision, explanation, snapshot, analysis and review hashes, experiments with plan,
  run, image id and tree hashes, package version, link to the previous record). Nothing changes while the state is unchanged;
  `--no-record` prints the history only. Every read re-hashes the objects and checks the chain links. Not provided: protection
  against deleting the whole workspace or the index rows (the workspace owner can), an MCP tool for the history.
- Concurrent writers: every state-changing operation holds one exclusive lock per workspace; a second process or thread is refused at
  once with `workspace is busy` (no waiting). The lock is released when its owner dies. Local filesystems only; network filesystems
  are unsupported. The lock does not make SQLite plus object files crash-atomic.
- Container output is limited while the container runs: stdout and stderr 16 MiB each, `/output` 256 MiB and 1000 entries (symlinks
  are not followed, hardlinks counted once, the larger of logical and allocated size is used). The runner polls every 0.1 s, kills
  the container by name, removes it and ends the client; the run is recorded as FAIL (`output limit exceeded`). This is a
  mitigation, not a quota. Narrowed, not closed: the poller also counts regular files on the output filesystem that container processes
  hold open after unlinking them (read from the host `/proc/<pid>/fd` of the container's process tree; the container's first PID
  comes from `podman inspect`), an unlinked file kept alive by a memory mapping fails the run (its size is not readable from the host),
  every file is capped at 64 MiB and every process at 1024 open files (`--ulimit fsize`, `--ulimit nofile`; the cap on file size also applies
  to `/tmp` and is a contract change for author code), and the limits are checked once more after the container exits. A scan of more
  than 16384 open files fails the run instead of stalling the watchdog. Still not covered: the unlinked-file count needs the container's
  PID and a readable `/proc` (an unreadable fd table fails the run, a missing PID disables only that count); it sees only processes in the
  container's tree; a file descriptor in flight over a unix socket (SCM_RIGHTS) is in no fd table and is bounded only by the per-file cap and
  the process and file-descriptor limits; and many hidden files each up to 64 MiB can still be written between two polls, so a hard disk
  guarantee does not exist. The container runs as your uid (root if you run as root); the memory limit does not set `--memory-swap`, so swap may add to it: a fast writer can exceed a limit by write speed times poll and stop delay, and stopping can take up to
  about 20 s. For a hard guarantee put the workspace on a volume with a filesystem quota.
- PDF text extraction runs in a fresh Linux interpreter with an empty environment and
  closed unrelated file descriptors. Before importing `pypdf`, the worker limits its
  address space to 512 MiB, CPU time to 15 s and each output file to 32 MiB, and disables
  core dumps. The caller kills and reaps it after 30 s or on interruption. Inputs and
  extracted UTF-8 text are limited to 32 MiB; encrypted PDFs, more than 300 pages and
  documents without extractable text are rejected. Complex PDFs may exceed these limits;
  supply reviewed Markdown/OCR instead. This is resource isolation, not a filesystem or
  network sandbox: the worker still runs under your account. PDF extraction does not
  establish visual review. The workspace writer lock remains held during import.
  `report` reads state without the writer lock, so a concurrent writer can make one report slightly stale.
- `project_benefit` compares measured values with independently declared numeric expectations. Alongside
  candidate-only guards (`op` and `value`), paired guards accept `metric`, `direction`
  (`higher_is_better` or `lower_is_better`), and `unit`. Both arms need numeric expectations.
  With `unit: absolute` or `relative_fraction`, set `min_gain`, `max_regression`, or both.
  Relative change divides the direction-adjusted delta by the absolute baseline. For a zero baseline,
  the default policy fails the relative check, and `zero_baseline_policy: require_non_worse` accepts
  zero or positive direction-adjusted change in place of the fractional bound. For `speedup_ratio`,
  use `lower_is_better` and `min_ratio`. Both measured values must be positive, and the ratio
  is baseline divided by candidate. Guard failure changes `benefit.met` independently of reproduction.
  These are single-run checks. For stochastic benchmarks, report independent repeats and uncertainty
  separately.
- An analysis citation may include a non-empty `claim` describing the specific statement it supports.
  Reports expose `citation_warnings` when a lexical check finds a possible negation, condition or
  unit mismatch. Reports flag missing claim links and nearby qualifiers as well. These signals are advisory.
  The success of the programs, the relevance of the measured proxy and the applicability of the claim
  remain separate questions for review.
- An environment HTTP proxy is ignored for the model endpoint, so the API key goes directly to the configured base URL.
- `export` writes a portable projection of the report, the plan and every run of each experiment
  (hashes, checks, expected/actual values, image ID and timings) and a manifest with hashes of the
  exported files. Source and input host paths become content references. The local store retains
  the original plans and decision objects. Their hashes identify the originals, and the manifest
  verifies the exported files. The archive omits log text and downloaded code. Known experiment
  paths, selected host path patterns and known experiment directory names are redacted in supported
  fields. Review arbitrary text, arguments, metadata and optional memory before sharing.

### Current and historical evidence

Current project decisions use attempts registered against the current analysis. Older
registrations remain visible as history. A changed container image marks each affected
attempt stale. Detailed and portable run records retain the original `score_status` and show
the current evidence status in `status`. Interrupted attempts remain in the history. Attempts are recorded before preflight. A new run,
export, or decision recording reconciles a leftover `RUNNING` attempt in the same job
under the workspace writer lock. Read-only views also report an orphaned attempt as
interrupted when the writer lock is idle. Later successful retries preserve the interruption
and keep mixed evidence visible.
If a benefit test passes against a proposed rejection, the project reports
`APPLICABILITY_CONFLICT` and calls for a revised assessment. Runs from older versions
that lack a seal bound to the specific attempt remain inspectable. Register and run a
new experiment for a current verdict. A legacy review without an evidence hash remains
in the audit record. Submit a passing review with the current packet hash to finalize
a verdict.

### Measuring workflow throughput

Use a fixed, pre-labelled batch of new candidates, the same input evidence and resource budget,
and a blind quality check. Compare completed, accepted decisions per hour and active specialist minutes
per accepted decision. Record queue wait and end-to-end latency separately, including opens, completions,
failures, blocked items, concurrency and model cost. Record queue/latency percentiles only where the
sample size supports them. Keep admitted reproduction cases, study selection and production benefit
as separate outcomes. The existing writer lock serializes runs sharing one workspace, so report whether
workloads share one workspace or use separately isolated workspaces. Counterbalance route order with
different operators or matched unseen candidates where possible to reduce carryover from prior study
review. Treat a small pilot as an exploratory estimate.

Project review tokens bind the job, both research and project snapshots, the declared
author and current experiment evidence. Reviews made with the earlier token format remain
in history and need a new review packet to approve the current decision. The adapter's
`handoff_status` describes qualification and the combined size of original sources.
The product also validates imported text documents during handoff.

The launcher retains the workspace lock while the Podman client is active. If the
runner dies, the launcher stops the client and removes its named container before
releasing the lock. Recovery then records the interrupted attempt. This behavior
requires the launcher process itself and the host kernel to remain operational.
