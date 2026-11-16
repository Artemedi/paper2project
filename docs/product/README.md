# Paper2Project

Import your project memories, import a research source, and get an evidence-backed answer to
"does this research apply to my project(s)?". Every step is stored in a local workspace, every
claim cites an exact snapshot line range, and a text-only opinion is never reported as a verified benefit.

Two ways to drive it, one implementation:

- **Host-agent mode**: `paper2project serve` is a stdio MCP server; your agent (Claude Code, etc.) does the reasoning.
- **Standalone mode**: `paper2project analyze` asks an OpenAI-compatible Chat Completions endpoint you configure.

Requirements: Python 3.11+. Optional: `pypdf` (`pip install '.[pdf]'`) to import PDFs; rootless Podman and a
local container image to run experiments. Linux x86_64 is the tested platform.

## Install and check

```bash
unzip paper2project-1.0.1.zip && cd paper2project-1.0.1
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
#   or: paper2project submit-analysis JOB analysis.json     # analysis written by you or an agent
paper2project review JOB --decision pass --notes "..." --reviewer "name" --analysis ANALYSIS_HASH   # hash from `report` JSON / job status
paper2project experiment add JOB plan.json
paper2project experiment run JOB EXPERIMENT
paper2project report JOB                                   # Markdown (or --format json)
paper2project export JOB out.zip                           # report + manifest; --include-memory adds private text
```

Re-importing a changed project or research source creates a new revision. Jobs stay bound to the
snapshots they were created from, are reported as **STALE** once the current memory differs, and refuse
to run experiments until you create a new job.

### Analysis format

One assessment per selected project. Each cites at least one passage of the research and one of that
project (`document_id` from `job context`, 1-based line range, and a required exact `quote` of at least 8 characters inside it).
`relevance` is `candidate`, `not_applicable` or `needs_evidence`. Citations are checked mechanically;
they prove the text exists, not that the reasoning is right, so use `review` with an independent reviewer.

### Standalone model mode

`--base-url` and `--model` are required (nothing is assumed). The API key is read from the environment
variable named by `--key-env` (default `PAPER2PROJECT_API_KEY`); it is never stored, logged or exported.
The command sends project and research text to that endpoint, so it refuses to run without `--yes-send`.
Rejected analyses are returned to the model with the validation error (3 attempts), never auto-accepted.

### Host-agent (MCP) mode

```json
{"mcpServers": {"paper2project": {"command": "/ABS/PATH/.venv/bin/paper2project",
  "args": ["--workspace", "/ABS/PATH/workspace", "serve", "--allow-root", "/ABS/PATH/memories"]}}}
```

Imports are limited to `--allow-root` directories (none given: all local imports denied). URL imports need
`--allow-network`; container experiments need `--allow-execution`; `analysis_review` needs `--allow-review`, which is off by
default so an agent confined to MCP cannot approve itself (use `paper2project review` from a separate session or person). This is
a gate on the MCP channel only: an agent with shell access to the same workspace can still run the CLI and name any reviewer, so the
review is a record of a human or separate-session decision, not a cryptographic proof of independence. Tools: `projects`, `research`, `project_import`,
`research_import`, `job_create`, `job_status`, `job_context`, `document_read`, `analysis_submit`, `analysis_review`,
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
  Benefit is demonstrated only if every check passes and the analysis has a passing independent `review` that names the current
  analysis hash and was recorded after every registered experiment plan (adding an experiment later leaves the claim at
  `BENEFIT_PENDING_REVIEW` until reviewed again). Show the reviewer the plans, sources and expected values, not just the prose.
  The input directory must contain data, and two metrics may not read the same result path.
- A `.git` directory in the source is compared with the declared revision (read without running git, regular files inside
  `.git` only); without one, the revision is recorded as unverified (`head_matches: false`). `head_matches: true` only
  means that `.git/HEAD` names the declared commit; it does not prove the working tree is clean or matches that commit.
  Symlinks and special files (FIFO, socket) in a source or input tree are refused. The whole tree, including `.git` and file modes, is hashed.
- Missing image, runtime, changed source/inputs, non-zero exits, missing `result.json` and failed checks are
  recorded as BLOCKED/FAIL with stdout, stderr, exit codes, timings and output hashes. Nothing skipped passes.
- Source or input changes after a run turn that evidence STALE.

Project ids come from file or directory names; a name with letters outside a-z gets a short hash suffix. Names that differ only in case or punctuation (`My App`, `my-app`) share an id: re-importing one under the other is refused unless you pass an explicit project or research id.

Project decisions in the report: `NOT_APPLICABLE`, `ANALYSIS_ONLY`, `EXPERIMENT_NOT_RUN`, `EXPERIMENT_BLOCKED`,
`EXPERIMENT_FAILED`, `REPRODUCED_BENEFIT_UNPROVEN`, `BENEFIT_PENDING_REVIEW`, `BENEFIT_DEMONSTRATED`,
`MIXED_EVIDENCE` (some experiments passed, others did not), `STALE`, `STALE_EVIDENCE`. Verdicts are re-derived from the
stored plan and result objects on every report. The per-arm exit code, error, timeout flag and result hash are sealed in a content-addressed object when the run is saved and read back from it (a run row without it never passes), so editing the row cannot turn a failure into a pass; an actor who can rewrite both the row and the objects can still change a verdict. The latest run
of each experiment counts; earlier runs are listed by count and kept in the export.
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
  mount only the copy, and its hash is checked again before every run, so later edits of the live directory neither change what
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
- A PDF is parsed in the application process: its text is size-limited only after extraction, so a crafted compact PDF can use much memory or time while holding the workspace lock. Import PDFs from sources you trust. `report` reads state without the writer lock, so a concurrent writer can make one report slightly stale.
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
  unit mismatch. These signals are advisory and independent review is still required for a benefit claim.
- An environment HTTP proxy is ignored for the model endpoint, so the API key goes directly to the configured base URL.
- `export` writes a portable projection of the report, the plan and every run of each experiment
  (hashes, checks, expected/actual values, image ID and timings) and a manifest with hashes of the
  exported files. Source and input host paths become content references. The local store retains
  the original plans and decision objects. Their hashes identify the originals, and the manifest
  verifies the exported files. The archive omits log text and downloaded code. Known experiment
  paths, selected host path patterns and known experiment directory names are redacted in supported
  fields. Review arbitrary text, arguments, metadata and optional memory before sharing.

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
