"""Source-bound container experiments scored by a host-owned comparator.

Source code runs only inside an offline, unprivileged, resource-limited Podman
container with its source and inputs read-only. The plan, scorer and workspace
state never enter the container, so the experiment cannot rewrite its own verdict.
"""
from __future__ import annotations

import hashlib
from fractions import Fraction
import math
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import stat
import subprocess
import sys
import time

from .findings import test_binding, artifact_anchor, evidence_key, finding_status, supported_tests
from .store import WorkspaceBusy, digest, dumps, loads, now, regular_file, unique

LIMITS = {"timeout": 120, "memory_mb": 1024, "cpus": 2, "pids": 128}
MAX_RESULT = 1024 * 1024
MAX_LOG = 256 * 1024
MAX_TREE_FILES = 20000
MAX_TREE_BYTES = 512 * 1024 * 1024
MAX_OUTPUT_FILE = 64 * 1024 * 1024
MAX_OPEN_FILES = 1024  # per process in the container; also bounds what the watchdog has to scan
MAX_SCANNED_FDS = 16384
MAX_OUTPUT_ENTRIES = 1000
MAX_STREAM = 16 * 1024 * 1024  # stdout and stderr each, enforced while the container runs
MAX_OUTPUT_TOTAL = 256 * 1024 * 1024
POLL_SECONDS = 0.1
PROC_EXIT_GRACE_MS = 20
KINDS = {"reproduction", "project_benefit"}
ARMS = ("baseline", "candidate")
IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,199}\Z")


def podman_command():
    override = os.environ.get("PAPER2PROJECT_PODMAN")
    if override:
        return shlex.split(override)
    if shutil.which("podman"):
        return ["podman"]
    if shutil.which("flatpak-spawn"):
        return ["flatpak-spawn", "--host", "podman"]
    if shutil.which("distrobox-host-exec"):
        return ["distrobox-host-exec", "podman"]
    raise ValueError("Rootless Podman is required (set PAPER2PROJECT_PODMAN to its command)")


def image_id(image):
    """Resolve the exact local image; never pull."""
    try:
        result = subprocess.run([*podman_command(), "image", "inspect", image, "--format", "{{.Id}}"],
                                capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("Container runtime unavailable: " + str(exc)) from exc
    if result.returncode or not result.stdout.strip():
        raise ValueError("Container image is not available locally: " + image)
    return result.stdout.strip()


def file_digest(path, limit=None):
    """Streaming SHA-256; refuses files above limit before reading them."""
    path = Path(path)
    hasher, size = hashlib.sha256(), 0
    with regular_file(path) as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            size += len(block)
            if limit is not None and size > limit:
                raise ValueError(f"File larger than {limit} bytes: {path.name}")
            hasher.update(block)
    return hasher.hexdigest()


def bounded_tree(root):
    paths = []
    for path in root.rglob("*"):
        if len(paths) >= MAX_TREE_FILES or len(path.relative_to(root).parts) > 32:
            raise ValueError("Experiment tree exceeds file or depth limits")
        paths.append(path)
    return sorted(paths)


def tree_hash(path):
    """Deterministic hash of everything the container can see: names, modes, content, .git included."""
    raw_root = Path(path)
    if raw_root.is_symlink() or not raw_root.is_dir():
        raise ValueError("Experiment tree must be a real directory: " + str(path))
    root = raw_root.resolve()
    entries, total = [], 0
    for item in bounded_tree(root):
        relative = item.relative_to(root).as_posix()
        if item.is_symlink():
            raise ValueError("Symlink in experiment tree: " + relative)
        mode = item.stat().st_mode & 0o7777
        if len(entries) >= MAX_TREE_FILES:
            raise ValueError("Experiment tree exceeds file or byte limits")
        if item.is_dir():
            entries.append((relative + "/", mode))
        elif item.is_file():
            total += item.stat().st_size
            if total > MAX_TREE_BYTES:
                raise ValueError("Experiment tree exceeds file or byte limits")
            entries.append((relative, mode, file_digest(item), item.stat().st_size))
        else:
            raise ValueError("Special file in experiment tree: " + relative)
    return {"sha256": digest(dumps(entries).encode("utf-8", "surrogatepass")), "files": len(entries), "bytes": total}


def _rmtree_writable(path):
    """Make snapshot directories writable before removing a failed tree."""
    root = Path(path)
    if not root.is_symlink():
        if root.is_dir():
            try:
                os.chmod(root, 0o700)
            except OSError:
                pass
        for dirpath, dirnames, _ in os.walk(root, topdown=True, followlinks=False):
            current = Path(dirpath)
            if not current.is_symlink():
                try:
                    os.chmod(current, 0o700)
                except OSError:
                    pass
            for name in dirnames:
                child = current / name
                if not child.is_symlink():
                    try:
                        os.chmod(child, 0o700)
                    except OSError:
                        pass
    shutil.rmtree(root, ignore_errors=True)


def copy_tree(source, destination, expected):
    """Copy a hashed tree into the workspace and prove the copy has exactly the hash that was registered.

    The container later mounts only the copy, so the live directory can no longer change what runs after it was hashed.
    Files are opened without following symlinks; modes are reproduced because they are part of the hash."""
    raw_source = Path(source)
    if raw_source.is_symlink() or not raw_source.is_dir():
        raise ValueError("Experiment tree must be a real directory: " + str(source))
    source, destination = raw_source.resolve(), Path(destination)
    destination.mkdir(parents=True, mode=0o700)
    directories = []
    copied = 0
    try:
        for item in bounded_tree(source):
            relative = item.relative_to(source)
            target = destination / relative
            mode = item.lstat().st_mode
            if stat.S_ISDIR(mode):
                target.mkdir(mode=0o700)
                directories.append((target, mode & 0o7777))
            elif stat.S_ISREG(mode):
                with regular_file(item) as read, open(target, "xb") as write:
                    for block in iter(lambda: read.read(1 << 20), b""):
                        copied += len(block)
                        if copied > MAX_TREE_BYTES:  # a file that keeps growing cannot fill the workspace
                            raise ValueError("Experiment tree exceeds file or byte limits while being copied")
                        write.write(block)
                os.chmod(target, mode & 0o7777)
            else:
                raise ValueError("Symlink or special file in experiment tree: " + relative.as_posix())
        for target, mode in sorted(directories, reverse=True):
            os.chmod(target, mode)
        if tree_hash(destination)["sha256"] != expected["sha256"]:
            raise ValueError("Experiment tree changed while it was being copied; register again from a quiet directory")
    except BaseException:
        _rmtree_writable(destination)
        raise
    return destination


def git_head(source, *, local=None):
    """Read checkout revision without running Git or following redirected metadata."""
    source = Path(source).resolve()
    pointer = source / ".git"

    def read(path, root, limit=4096):
        if path.resolve() != path or not path.is_relative_to(root) or not path.is_file():
            return None
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        return data.decode() if len(data) <= limit else None

    try:
        if pointer.is_symlink():
            return None
        git = common = pointer
        if pointer.is_file():
            directive = (read(pointer, Path(source)) or "").strip()
            if not directive.startswith("gitdir: "):
                return None
            target = Path(source) / directive.removeprefix("gitdir: ")
            git = Path(os.path.abspath(target))
            if ((local is not None and Path(local(git)) != git) or git.resolve() != git
                    or git.parent.name != "worktrees"):
                return None
            common = git.parent.parent
            if local is not None and Path(local(common)) != common:
                return None
            if (read(git / "commondir", git) or "").strip() != "../..":
                return None
            backlink = (read(git / "gitdir", git) or "").strip()
            if not backlink or Path(os.path.abspath(git / backlink)) != pointer:
                return None
        elif not pointer.is_dir():
            return None
        head = (read(git / "HEAD", git) or "").strip()
        found = None
        if re.fullmatch(r"[0-9a-f]{40}", head):
            found = head
        elif head.startswith("ref: refs/") and ".." not in head:
            ref = head[5:]
            value = read(common / ref, common)
            if value is not None:
                found = value.strip()
            else:
                for line in (read(common / "packed-refs", common, 8 << 20) or "").splitlines():
                    stripped = line.strip()
                    if not stripped or stripped.startswith(("#", "^")):
                        continue
                    parts = stripped.split(" ", 1)
                    if len(parts) == 2 and parts[1] == ref:
                        found = parts[0]
        return found if found and re.fullmatch(r"[0-9a-f]{40}", found) else None
    except (OSError, UnicodeError, ValueError, RuntimeError):
        return None


def strict_equal(actual, expected):
    if type(actual) is not type(expected):
        return False
    if isinstance(actual, dict):
        return actual.keys() == expected.keys() and all(strict_equal(actual[k], expected[k]) for k in actual)
    if isinstance(actual, list):
        return len(actual) == len(expected) and all(strict_equal(x, y) for x, y in zip(actual, expected))
    return actual == expected


def extract(value, path):
    for part in path.split("."):
        if isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise KeyError(path)
    return value


def matches(actual, expected, comparison, tolerance):
    if comparison == "exact":
        return strict_equal(actual, expected)
    if not (number(actual) and number(expected) and number(tolerance) and tolerance >= 0):
        return False
    try:
        tol = Fraction(str(tolerance))
        exp = Fraction(str(expected))
        act = Fraction(str(actual))
        bound = tol if comparison == "abs" else tol * abs(exp)
        spread = abs(act - exp)
        return spread <= bound
    except (OverflowError, ValueError):
        return False


def number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an integer too large for a float is not a usable number
        return False


def validate_paired_guard(guard, metric):
    unit = guard["unit"]
    direction = guard.get("direction")
    zero_policy = guard.get("zero_baseline_policy", "fail")
    if direction not in ("higher_is_better", "lower_is_better"):
        raise ValueError("Paired guard needs a metric direction")
    if unit not in ("absolute", "relative_fraction", "speedup_ratio"):
        raise ValueError("Invalid paired guard unit")
    if zero_policy not in ("fail", "require_non_worse"):
        raise ValueError("Invalid zero_baseline_policy")
    if set(metric["expected"]) != set(ARMS) or not all(number(v) for v in metric["expected"].values()):
        raise ValueError("Paired guard needs numeric expected values for both arms")
    if unit == "speedup_ratio":
        if (direction != "lower_is_better" or not number(guard.get("min_ratio")) or guard["min_ratio"] <= 0
                or zero_policy != "fail" or {"min_gain", "max_regression", "op", "value"} & guard.keys()):
            raise ValueError("Speedup guard needs lower_is_better, a positive min_ratio and positive measurements")
    else:
        thresholds = [guard[key] for key in ("min_gain", "max_regression") if key in guard]
        if not thresholds or not all(number(v) and v >= 0 for v in thresholds) or {"min_ratio", "op", "value"} & guard.keys():
            raise ValueError("Paired guard needs a non-negative min_gain or max_regression")


def paired_guard(guard, base, candidate):
    result = {**guard, "baseline": base if number(base) else None,
              "actual": candidate if number(candidate) else None, "passed": False}
    if result["baseline"] is None or result["actual"] is None:
        return {**result, "reason": "NON_FINITE_OR_MISSING_METRIC"}
    unit = guard["unit"]
    base_decimal, candidate_decimal = Fraction(str(base)), Fraction(str(candidate))
    delta_decimal = (candidate_decimal - base_decimal if guard["direction"] == "higher_is_better"
                     else base_decimal - candidate_decimal)
    try:
        if unit == "speedup_ratio":
            if base <= 0 or candidate <= 0:
                return {**result, "reason": "NON_POSITIVE_SPEEDUP_METRIC"}
            ratio_decimal = base_decimal / candidate_decimal
            ratio = ratio_decimal.numerator if ratio_decimal.denominator == 1 else float(ratio_decimal)
            if not number(ratio):
                return {**result, "reason": "NON_FINITE_COMPARISON"}
            return {**result, "ratio": ratio, "ratio_exact": str(ratio_decimal),
                    "passed": ratio_decimal >= Fraction(str(guard["min_ratio"]))}
        if unit == "relative_fraction" and base == 0:
            if guard.get("zero_baseline_policy") == "require_non_worse":
                return {**result, "reason": "ZERO_BASE_NON_WORSE",
                        "passed": delta_decimal >= 0 and guard.get("min_gain", 0) == 0}
            return {**result, "reason": "ZERO_BASELINE_FOR_RELATIVE_METRIC"}
        change_decimal = delta_decimal if unit == "absolute" else delta_decimal / abs(base_decimal)
        change = change_decimal.numerator if change_decimal.denominator == 1 else float(change_decimal)
    except OverflowError:
        return {**result, "reason": "NON_FINITE_COMPARISON"}
    if not number(change):
        return {**result, "reason": "NON_FINITE_COMPARISON"}
    passed = ("min_gain" not in guard or change_decimal >= Fraction(str(guard["min_gain"]))) and (
        "max_regression" not in guard or -change_decimal <= Fraction(str(guard["max_regression"])))
    return {**result, "change": change, "change_exact": str(change_decimal), "passed": passed}


def validate_plan(plan, project_ids):
    if not isinstance(plan, dict):
        raise ValueError("Experiment plan must be an object")
    if plan.get("kind") not in KINDS:
        raise ValueError("kind must be reproduction or project_benefit")
    if plan.get("project_id") not in project_ids:
        raise ValueError("Experiment project is not part of this job")
    if not isinstance(plan.get("image"), str) or not IMAGE.fullmatch(plan["image"]):
        raise ValueError("image must be a local container image reference")
    source = plan.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("path"), str) \
            or not source["path"].strip() or not re.fullmatch(r"[0-9a-f]{40}", str(source.get("revision", ""))) \
            or not isinstance(source.get("license"), str) or not source["license"].strip():
        raise ValueError("source needs path, 40-hex revision and license")
    if plan.get("input") is not None and (not isinstance(plan["input"], dict) or not isinstance(plan["input"].get("path"), str)
                                          or not plan["input"]["path"].strip()):
        raise ValueError("input must be {path}")
    if not isinstance(plan.get("arms"), dict):
        raise ValueError("arms must be {baseline, candidate}")
    for arm in ARMS:
        argv = plan["arms"].get(arm)
        if not isinstance(argv, list) or not 1 <= len(argv) <= 32 or not all(isinstance(a, str) and a and "\0" not in a and len(a) <= 4096 for a in argv):
            raise ValueError(f"arms.{arm} must be a command list of 1..32 non-empty strings")
    metrics = plan.get("metrics")
    if not isinstance(metrics, list) or not 1 <= len(metrics) <= 50:
        raise ValueError("Declare 1..50 metrics")
    names, paths = set(), []
    if not isinstance(plan.get("limits", {}), dict):
        raise ValueError("limits must be an object")
    for metric in metrics:
        if not isinstance(metric, dict) or not isinstance(metric.get("name"), str) or not metric["name"] or metric["name"] in names:
            raise ValueError("Each metric needs a unique name")
        names.add(metric["name"])
        paths.append(metric.get("path"))
        if not isinstance(metric.get("path"), str) or not re.fullmatch(r"[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*", metric["path"]):
            raise ValueError("metric.path must be a dotted result path")
        if any(re.fullmatch(r"0[0-9]+", part) for part in metric["path"].split(".")):
            raise ValueError("metric.path cannot use array indexes with leading zeros")
        comparison = metric.get("comparison", "exact")
        if comparison not in {"exact", "abs", "rel"}:
            raise ValueError("comparison must be exact, abs or rel")
        if comparison != "exact" and not (number(metric.get("tolerance")) and metric["tolerance"] >= 0):
            raise ValueError("abs/rel comparison needs a non-negative tolerance")
        expected = metric.get("expected")
        if not isinstance(expected, dict) or "candidate" not in expected or set(expected) - set(ARMS):
            raise ValueError("metric.expected needs an independent value for the candidate (baseline optional)")
        for value in expected.values():
            if comparison != "exact" and not number(value):
                raise ValueError("Tolerance comparison needs finite numeric expected values")
    if len(set(paths)) != len(paths):
        raise ValueError("Each metric must read a different result path")
    if plan["kind"] == "project_benefit":
        benefit = plan.get("benefit")
        if not isinstance(benefit, dict) or benefit.get("direction") not in {"increase", "decrease"} \
                or not number(benefit.get("min_delta")) or benefit["min_delta"] <= 0:
            raise ValueError("project_benefit needs benefit {metric, direction, min_delta > 0}")
        if not plan.get("input"):
            raise ValueError("project_benefit needs an input directory with the target data")
        if plan["arms"]["baseline"] == plan["arms"]["candidate"]:
            raise ValueError("baseline and candidate commands must differ")
        if not isinstance(benefit.get("guards"), list) or not benefit["guards"]:
            raise ValueError("project_benefit needs at least one quality/cost guard")
        target = next((m for m in metrics if m["name"] == benefit.get("metric")), None)
        if target is None or set(target["expected"]) != set(ARMS) or not number(target["expected"]["baseline"]) or not number(target["expected"]["candidate"]):
            raise ValueError("Benefit metric must be numeric with independent expected values for both arms")
        slack = Fraction(0)
        if target.get("comparison", "exact") != "exact":  # a loose tolerance would let any number "match" the expectation
            scale = Fraction(1) if target["comparison"] == "abs" else max(
                abs(Fraction(str(target["expected"]["baseline"]))),
                abs(Fraction(str(target["expected"]["candidate"]))),
            )
            slack = Fraction(str(target["tolerance"])) * scale
        if 2 * slack >= Fraction(str(benefit["min_delta"])):
            raise ValueError("Benefit metric tolerance must be below half of min_delta")
        for guard in benefit["guards"]:
            if not isinstance(guard, dict) or not isinstance(guard.get("metric"), str) or guard["metric"] not in names:
                raise ValueError("Each guard needs a declared metric")
            if guard["metric"] == benefit.get("metric"):
                raise ValueError("A guard must check a different metric than the benefit metric")
            if "unit" in guard:
                validate_paired_guard(guard, next(m for m in metrics if m["name"] == guard["metric"]))
            elif guard.get("op") not in ("le", "ge") or not number(guard.get("value")):
                raise ValueError("Each candidate guard needs op le/ge and numeric value")
    limits = {**LIMITS, **plan.get("limits", {})}
    if not (set(limits) == set(LIMITS) and all(number(v) for v in limits.values())
            and 1 <= limits["timeout"] <= 3600 and 64 <= limits["memory_mb"] <= 16384 and 0.1 <= limits["cpus"] <= 16 and 8 <= limits["pids"] <= 1024):
        raise ValueError("limits out of range")
    return {**plan, "limits": limits}


def benefit_reference(app, project, reference, plan, source_tree, input_tree):
    """Bind the declared expectations to an artifact frozen in the job snapshot."""
    if not isinstance(reference, str):
        raise ValueError("reference must name a frozen project attachment")
    asset = app.store.json(project["snapshot"]).get("assets", {}).get(reference)
    if asset is None:
        raise ValueError("reference must name a frozen project attachment")
    receipt = app.store.json(asset["sha256"])
    expected = {metric["name"]: metric["expected"] for metric in plan["metrics"]}
    if (not isinstance(receipt, dict) or type(receipt.get("schema")) is not int or receipt["schema"] != 1
            or not isinstance(receipt.get("method"), str) or not receipt["method"].strip()
            or receipt.get("source_tree") != source_tree["sha256"]
            or receipt.get("input_tree") != input_tree["sha256"]
            or dumps(receipt.get("expected")) != dumps(expected)):
        raise ValueError("reference must document its method and match the source, inputs and expected values")
    return asset["sha256"]


def add_experiment(app, job_id, plan):
    job = app.store.get("job", job_id)
    if not job["analysis"]:
        raise ValueError("Submit a grounded analysis before registering experiments")
    plan = validate_plan(plan, {p["id"] for p in job["projects"]})
    assessment = next(a for a in app.store.json(job["analysis"])["assessments"] if a["project_id"] == plan["project_id"])
    project = next(p for p in job["projects"] if p["id"] == plan["project_id"])
    if assessment["relevance"] == "findings_only":
        if not isinstance(plan.get("finding_id"), str) or plan["finding_id"] not in {row["id"] for row in assessment["findings"]}:
            raise ValueError("Scoped experiments must select a finding_id from this project")
        provenance = plan.get("provenance")
        if not isinstance(provenance, dict) or not all(
                isinstance(provenance.get(key), str) and provenance[key].strip() for key in ("data", "comparison")):
            raise ValueError("Scoped experiments need provenance describing the data and comparison")
        assets = app.store.json(project["snapshot"]).get("assets", {})
        oracle = artifact_anchor(provenance.get("oracle"), assets, app.store.blob)
        plan = {**plan, "provenance": {"oracle": oracle, "data": provenance["data"], "comparison": provenance["comparison"]}}
    elif "finding_id" in plan:
        raise ValueError("finding_id requires a scoped analysis")
    source = Path(app.local(plan["source"]["path"]))
    inputs = Path(app.local(plan["input"]["path"])) if plan.get("input") else None
    mount_safe(source)
    if inputs:
        mount_safe(inputs)
    source_tree = tree_hash(source)  # rejects symlinks and special files before anything inside is read
    input_tree = tree_hash(inputs) if inputs else None
    if plan["kind"] == "project_benefit" and not input_tree["bytes"]:
        raise ValueError("project_benefit needs non-empty target data in the input directory")
    head = git_head(source, local=app.local)
    if head is not None and head != plan["source"]["revision"]:
        raise ValueError(f"Source checkout is at {head}, not the declared revision {plan['source']['revision']}")
    reference = None
    if "reference" in plan:
        if plan["kind"] != "project_benefit" or head is None:
            raise ValueError("A benefit reference requires project_benefit and a verified Git source revision in a supported checkout")
        reference = benefit_reference(app, project, plan["reference"], plan, source_tree, input_tree)
    experiment_id = unique("exp")
    snapshot_dir = app.store.root / "snapshots" / experiment_id
    source_copy = copy_tree(source, snapshot_dir / "source", source_tree)
    if head is not None and git_head(source, local=app.local) != head:
        _rmtree_writable(snapshot_dir)
        raise ValueError("Source Git revision changed during registration")
    try:
        inputs = copy_tree(inputs, snapshot_dir / "input", input_tree) if inputs else None
    except BaseException:
        _rmtree_writable(snapshot_dir)
        raise
    entry = {"id": experiment_id, "job_id": job_id, "project_id": plan["project_id"], "kind": plan["kind"],
             "plan": app.store.put_json(plan), "created_at": now(), "head_matches": head is not None, "analysis": job["analysis"],
             "source_tree": source_tree, "input_tree": input_tree,
             "source_path": str(source_copy.resolve()), "input_path": str(inputs.resolve()) if inputs else None,
             "origin_source_path": plan["source"]["path"], "origin_input_path": plan["input"]["path"] if plan.get("input") else None}
    if reference:
        entry["reference_sha256"] = reference
    if plan.get("finding_id"):
        entry.update(finding_id=plan["finding_id"], oracle_sha256=plan["provenance"]["oracle"]["sha256"])
    app.store.save("experiment", entry["id"], entry)
    app.event(job_id, "experiment", {"experiment": entry["id"], "plan": entry["plan"]})
    return entry


def experiments(app, job_id):
    return [e for e in app.store.list("experiment") if e["job_id"] == job_id]


SEALED = ("exit_code", "timed_out", "error", "result", "result_sha256")


def run_identity(run):
    return {key: run.get(key) for key in ("id", "experiment_id", "job_id", "plan", "source_tree", "input_tree", "image_id")}


def evaluate(app, run):
    """Re-derive the verdict from the plan and stored result objects; the run row's own status is not trusted."""
    arms = run.get("arms") or {}
    if not arms:
        status = run.get("status")
        return {**run, "status": status if status in {"BLOCKED", "RUNNING", "INTERRUPTED"} else "FAIL"}
    if not run.get("sealed"):  # facts that decide the verdict must come from a hashed object, not from an editable row
        if run.get("status") in {"BLOCKED", "RUNNING", "INTERRUPTED"}:
            return {**run, "checks": [], "reproduced": False, "benefit": None}  # partial arms cannot establish PASS
        return {**run, "status": "FAIL", "checks": [], "reproduced": False, "benefit": None,
                "reason": run.get("reason") if run.get("status") == "FAIL" and run.get("reason") else "run has no sealed arm facts"}
    seal = app.store.json(run["sealed"])  # rehashed on read
    if seal.get("schema") != 2:
        return {**run, "status": "STALE", "checks": [], "reproduced": False, "benefit": None,
                "reason": "Legacy seal has no run identity; register and run a new experiment"}
    facts = seal.get("arms", {})
    if seal.get("run") != run_identity(run) or set(facts) != set(arms) or set(arms) != set(ARMS):
        return {**run, "status": "FAIL", "reproduced": False, "benefit": None, "reason": "sealed run identity or arms disagree with the run row"}
    plan = app.store.json(run["plan"])
    verified = {arm: {**record, **facts[arm]} for arm, record in arms.items()}
    loaded = {arm: {**record, "result": app.store.json(record["result"]) if record.get("result") else None}
              for arm, record in verified.items()}
    checks, reproduced, benefit = score(plan, loaded)
    return {**run, "arms": verified, "checks": checks, "reproduced": reproduced, "benefit": benefit, "status": "PASS" if reproduced else "FAIL"}


def runs(app, experiment_id):
    experiment = app.store.get("experiment", experiment_id)
    rows = (r for r in app.store.list("run", insertion_order=True) if r["experiment_id"] == experiment_id)
    confirmed = []
    for row in rows:
        if any(row[field] != experiment[field] for field in ("job_id", "plan", "source_tree", "input_tree")):
            confirmed.append({**row, "status": "FAIL", "reproduced": False, "benefit": None,
                              "checks": [], "reason": "Run record does not match the registered experiment"})
        else:
            confirmed.append(evaluate(app, row))
    return confirmed


def mount_safe(path):
    """Mount specs are colon/comma separated; reject paths that could smuggle options."""
    text = str(path)
    if any(c in text for c in ":,\n\0"):
        raise ValueError("Path contains a character unsafe for container mounts: " + text)
    return text


def container_argv(limits, image, command, source, input_path, output, name=None):
    argv = [*podman_command(), "run", "--rm", *(["--name", name] if name else []), "--pull=never", "--network=none", "--read-only",
            "--userns=keep-id", "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--pids-limit={limits['pids']}", f"--memory={limits['memory_mb']}m", f"--cpus={limits['cpus']}",
            f"--timeout={limits['timeout']}", "--ulimit", f"fsize={MAX_OUTPUT_FILE}:{MAX_OUTPUT_FILE}",
            "--ulimit", f"nofile={MAX_OPEN_FILES}:{MAX_OPEN_FILES}",
            "--tmpfs", "/tmp:rw,nosuid,noexec,size=256m",
            "--entrypoint", "", "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1",
            "-v", f"{mount_safe(source)}:/source:ro,Z", "-v", f"{mount_safe(output)}:/output:rw,Z", "--workdir", "/source"]
    if input_path:
        argv += ["-v", f"{mount_safe(input_path)}:/input:ro,Z"]
    return [*argv, image, *command]


def read_log(path):
    with path.open("rb") as stream:
        return stream.read(MAX_LOG).decode("utf-8", "replace")


def collect(record, out, stdout, stderr):
    """Hash and parse container output without following symlinks or special files."""
    record["stdout"], record["stderr"] = read_log(stdout), read_log(stderr)
    record["stdout_sha256"], record["stderr_sha256"] = file_digest(stdout), file_digest(stderr)
    files, entries = {}, 0
    root_fd = os.open(out, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        pinned = os.fstat(root_fd)
        for directory, folders, names, descriptor in os.fwalk(".", dir_fd=root_fd, follow_symlinks=False):
            for name in folders + names:
                entries += 1
                if entries > MAX_OUTPUT_ENTRIES:
                    record["error"] = record["error"] or "Output has more than 1000 entries"
                    break
                relative = (Path(directory) / name).as_posix()
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    continue
                if not stat.S_ISREG(info.st_mode):
                    record["error"] = record["error"] or ("Output contains a symlink or special file: " + relative)
                    continue
                hasher, size = hashlib.sha256(), 0
                with os.fdopen(os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                      dir_fd=descriptor), "rb") as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise ValueError("Output entry changed to a special file: " + relative)
                    for block in iter(lambda: stream.read(1 << 20), b""):
                        size += len(block)
                        if size > MAX_OUTPUT_FILE:
                            break
                        hasher.update(block)
                if size > MAX_OUTPUT_FILE:
                    files[relative] = None
                    record["error"] = record["error"] or f"Output file {relative} exceeds {MAX_OUTPUT_FILE} bytes"
                else:
                    files[relative] = hasher.hexdigest()
            if entries > MAX_OUTPUT_ENTRIES:
                break
        record["output_files"] = files
        try:
            current = os.stat(out, follow_symlinks=False)
        except OSError:
            raise ValueError("Output root changed during collection")
        if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise ValueError("Output root changed during collection")
        if record["exit_code"] == 0 and not record["error"]:
            with os.fdopen(os.open("result.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                  dir_fd=root_fd), "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError("result.json must be a regular file")
                raw = stream.read(MAX_RESULT + 1)
            if len(raw) > MAX_RESULT:
                raise ValueError("result.json missing, unsafe or larger than 1 MiB")
            record["result"], record["result_sha256"] = loads(raw), digest(raw)
    finally:
        os.close(root_fd)


def container_root_pid(name):
    """Host PID of the container's first process, or None while it is not running (or podman cannot say)."""
    try:
        done = subprocess.run([*podman_command(), "inspect", "--format", "{{.State.Pid}}", name], capture_output=True, text=True, timeout=5)
        pid = int(done.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return pid if pid > 0 else None


def process_tree(root):
    """The process and all of its descendants, as seen in the host /proc."""
    pids, stack = [], [root]
    while stack:
        pid = stack.pop()
        if pid in pids:
            continue
        pids.append(pid)
        try:
            for task in os.listdir(f"/proc/{pid}/task"):
                with open(f"/proc/{pid}/task/{task}/children") as handle:
                    stack += [int(item) for item in handle.read().split()]
        except (FileNotFoundError, ProcessLookupError):
            pass
    return pids


def process_exited(pid):
    """Use a process pidfd to confirm that the whole thread group has exited."""
    try:
        fd = os.pidfd_open(pid)
    except ProcessLookupError:
        return True
    except (AttributeError, OSError):
        # No reliable exit signal on this host: preserve the inspection failure.
        return False
    try:
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        # procfs can revoke access just before the process descriptor signals exit.
        return any(flags & (select.POLLIN | select.POLLHUP)
                   for _, flags in poller.poll(PROC_EXIT_GRACE_MS))
    finally:
        os.close(fd)


def deleted_open_bytes(pids, device):
    """Bytes held by regular files that these processes keep open after unlinking them, on the output filesystem.
    A directory walk cannot see them, but they still occupy the disk. An unreadable fd table for a live process raises PermissionError."""
    total, seen, scanned = 0, set(), 0
    for pid in pids:
        try:
            fds = os.listdir(f"/proc/{pid}/fd")
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            if process_exited(pid):
                continue
            raise
        scanned += len(fds)
        if scanned > MAX_SCANNED_FDS:  # a scan that long would stall the watchdog: fail closed instead
            raise OSError(f"more than {MAX_SCANNED_FDS} open files to inspect")
        for fd in fds:
            try:
                info = os.stat(f"/proc/{pid}/fd/{fd}")
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                if process_exited(pid):
                    break
                raise
            if info.st_dev != device or info.st_nlink != 0 or not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) in seen:
                continue
            seen.add((info.st_dev, info.st_ino))
            total += max(info.st_size, info.st_blocks * 512)
    return total


def deleted_mapping(pids, device, roots=()):
    """True if a container process maps a file that was unlinked on the output filesystem. Its size cannot be read from the host
    (map_files is not accessible), so the mapping is treated as a breach rather than counted."""
    want = f"{os.major(device):02x}:{os.minor(device):02x}"
    # Btrfs can expose different device numbers in stat and procfs.
    prefixes = tuple(str(root).rstrip("/") + "/" for root in roots)
    for pid in pids:
        try:
            with open(f"/proc/{pid}/maps") as handle:
                for line in handle:
                    fields = line.split(None, 5)
                    if len(fields) == 6:
                        path = fields[5].removesuffix("\n")
                        if path.endswith(" (deleted)") and (fields[3] == want or path.startswith(prefixes)):
                            return True
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            if not process_exited(pid):
                raise
    return False


def output_breach(stdout, stderr, out, pids=()):
    """Reason the running container exceeded its output budget, else None. Never follows symlinks; counts hardlinks once.
    `pids` are the container's host processes; files they hold open after unlinking count towards the total."""
    for path in (stdout, stderr):
        if path.stat().st_size > MAX_STREAM:
            return f"{path.name} larger than {MAX_STREAM} bytes"
    total = entries = 0
    seen = set()
    def fail_walk(error):
        raise error

    # Pin the output root itself: fwalk(path) silently skips a symlink at its root.
    try:
        root_fd = os.open(out, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return "output directory is missing or unsafe"
    try:
        root_info = os.fstat(root_fd)
        for _, directories, files, directory in os.fwalk(".", dir_fd=root_fd, follow_symlinks=False, onerror=fail_walk):
            for name in directories + files:
                entries += 1
                if entries > MAX_OUTPUT_ENTRIES:
                    return f"more than {MAX_OUTPUT_ENTRIES} output entries"
                try:
                    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(info.st_mode):
                    return "output contains a symlink"
                if not stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) not in seen:
                    seen.add((info.st_dev, info.st_ino))
                    total += max(info.st_size, info.st_blocks * 512)  # logical or allocated, whichever is larger (sparse files)
            if total > MAX_OUTPUT_TOTAL:
                return f"output directory larger than {MAX_OUTPUT_TOTAL} bytes"
        try:
            current = os.stat(out, follow_symlinks=False)
        except OSError:
            return "output directory changed during scan"
        if (current.st_dev, current.st_ino) != (root_info.st_dev, root_info.st_ino):
            return "output directory changed during scan"
        if pids:
            device = root_info.st_dev
            if deleted_mapping(pids, device, (str(Path(out).absolute()), "/output")):
                return "an unlinked file is kept alive by a memory mapping"
            if total + deleted_open_bytes(pids, device) > MAX_OUTPUT_TOTAL:
                return f"output (including unlinked open files) larger than {MAX_OUTPUT_TOTAL} bytes"
    finally:
        os.close(root_fd)
    return None


class CleanupPending(RuntimeError):
    """The attempt must stay recoverable until container cleanup can be confirmed."""


def remove_container(name):
    try:
        command = podman_command()
        result = subprocess.run([*command, "container", "exists", name], capture_output=True, timeout=10)
        if result.returncode == 0:
            subprocess.run([*command, "rm", "-f", "--time", "1", name], capture_output=True, timeout=20)
            result = subprocess.run([*command, "container", "exists", name], capture_output=True, timeout=10)
        if result.returncode != 1:
            raise CleanupPending("Unable to confirm container removal")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise CleanupPending("Unable to confirm container removal") from exc


def stop_container(proc, name, control_fd):
    """Signal the launcher first and keep an unconfirmed cleanup recoverable."""
    os.close(control_fd)
    try:
        proc.wait(timeout=35)
    except subprocess.TimeoutExpired as exc:
        raise CleanupPending("Launcher has not finished stopping the container") from exc
    except (KeyboardInterrupt, SystemExit) as exc:
        raise CleanupPending("Launcher cleanup was interrupted") from exc
    try:
        remove_container(name)
    except (KeyboardInterrupt, SystemExit) as exc:
        raise CleanupPending("Container cleanup was interrupted") from exc


def run_arm(app, plan, image, experiment, run_dir, arm):
    out = run_dir / arm / "output"
    out.mkdir(parents=True, mode=0o700)
    name = "p2p-" + run_dir.name + "-" + arm
    run = app.store.get("run", run_dir.name)
    app.store.save("run", run_dir.name, {**run, "active_container": name})
    argv = container_argv(plan["limits"], image, plan["arms"][arm], experiment["source_path"], experiment["input_path"], out, name)
    stdout, stderr = run_dir / arm / "stdout.txt", run_dir / arm / "stderr.txt"
    started = time.monotonic()
    record = {"arm": arm, "exit_code": None, "timed_out": False, "error": None, "result": None, "result_sha256": None,
              "output_files": {}, "stdout": "", "stderr": "", "stdout_sha256": None, "stderr_sha256": None}
    try:
        with stdout.open("wb") as o, stderr.open("wb") as e:
            # A surviving launcher retains the workspace lock until the Podman client exits.
            # Recovery cannot mistake the launch window for an absent container.
            lock_fd = app.store._lock.fd
            if lock_fd is None:
                raise ValueError("Container execution requires the workspace writer lock")
            read_fd, write_fd = os.pipe()
            launcher = """
import os, select, signal, subprocess, sys
read_fd = int(sys.argv[1])
name = sys.argv[2]
podman_count = int(sys.argv[3])
command = sys.argv[4:]
poller = select.poll()
poller.register(read_fd, select.POLLIN | select.POLLHUP | select.POLLERR)
client = subprocess.Popen(command, start_new_session=True)
try:
    while client.poll() is None:
        ready = poller.poll(100)
        if not ready:
            continue
        if not os.read(read_fd, 1):
            raise EOFError("Runner stopped")
except BaseException:
    if client.poll() is None:
        try:
            os.killpg(client.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        client.wait()
        try:
            subprocess.run([*command[:podman_count], "kill", name],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            subprocess.run([*command[:podman_count], "rm", "-f", "--time", "1", name],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        except (OSError, subprocess.SubprocessError):
            pass
    raise SystemExit(1)
raise SystemExit(client.returncode)
"""
            try:
                proc = subprocess.Popen([sys.executable, "-c", launcher, str(read_fd), name,
                                         str(len(podman_command())), *argv],
                                        stdout=o, stderr=e, stdin=subprocess.DEVNULL,
                                        start_new_session=True, pass_fds=(lock_fd, read_fd))
            except BaseException:
                os.close(write_fd)
                raise
            finally:
                os.close(read_fd)
            deadline = time.monotonic() + plan["limits"]["timeout"] + 30  # the container has its own --timeout; the extra time is cleanup
            def stop():
                nonlocal write_fd
                control_fd, write_fd = write_fd, None
                stop_container(proc, name, control_fd)

            root_pid = None
            try:
                while True:
                    try:
                        record["exit_code"] = proc.wait(timeout=POLL_SECONDS)
                        if write_fd is not None:
                            os.close(write_fd)
                            write_fd = None
                        if record["exit_code"] != 0:
                            remove_container(name)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    try:
                        root_pid = root_pid or container_root_pid(name)
                        breach = output_breach(stdout, stderr, out, process_tree(root_pid) if root_pid else ())
                    except OSError as exc:  # an output tree the watchdog cannot read is not an output tree it can ignore
                        breach = "output directory cannot be inspected (" + type(exc).__name__ + ")"
                    if breach or time.monotonic() > deadline:
                        stop()
                        record["exit_code"] = proc.returncode
                        if breach:
                            record["error"] = "output limit exceeded: " + breach
                        else:
                            record["timed_out"] = True
                        break
            except BaseException:  # KeyboardInterrupt and the like must not leave a running container without a watchdog
                if write_fd is not None:
                    stop()
                raise
            finally:
                if write_fd is not None:
                    os.close(write_fd)
    except OSError as exc:
        record["error"] = str(exc)
    if not record["error"]:  # the last write can land between the final poll and the exit
        try:
            breach = output_breach(stdout, stderr, out)
        except OSError as exc:
            breach = "output directory cannot be inspected (" + type(exc).__name__ + ")"
        if breach:
            record["error"] = "output limit exceeded: " + breach
    record["seconds"] = round(time.monotonic() - started, 3)
    try:
        collect(record, out, stdout, stderr)
    except Exception as exc:  # RecursionError, MemoryError, ValueError...: keep the run, never pass it
        record["result"], record["result_sha256"] = None, None
        record["error"] = record["error"] or f"Invalid container output ({type(exc).__name__}): {exc}"
    return record


def score(plan, arms):
    checks = []
    for metric in plan["metrics"]:
        for arm in ARMS:
            if arm not in metric["expected"]:
                continue
            result = arms[arm]["result"]
            check = {"metric": metric["name"], "arm": arm, "expected": metric["expected"][arm], "actual": None, "passed": False}
            try:
                check["actual"] = extract(result, metric["path"]) if result is not None else None
                check["passed"] = result is not None and matches(check["actual"], check["expected"], metric.get("comparison", "exact"), metric.get("tolerance", 0))
            except KeyError:
                check["error"] = "metric path missing from result"
            checks.append(check)
    reproduced = all(c["passed"] for c in checks) and all(a["exit_code"] == 0 and not a["error"] and not a["timed_out"] for a in arms.values())
    benefit = None
    if plan["kind"] == "project_benefit":
        spec = plan["benefit"]
        base = next((c["actual"] for c in checks if c["metric"] == spec["metric"] and c["arm"] == "baseline"), None)
        cand = next((c["actual"] for c in checks if c["metric"] == spec["metric"] and c["arm"] == "candidate"), None)
        delta = None
        met = False
        if number(base) and number(cand):
            delta_decimal = (Fraction(str(cand)) - Fraction(str(base)) if spec["direction"] == "increase"
                             else Fraction(str(base)) - Fraction(str(cand)))
            try:
                delta = delta_decimal.numerator if delta_decimal.denominator == 1 else float(delta_decimal)
            except OverflowError:
                delta = None
            delta = delta if number(delta) else None
            met = delta is not None and delta_decimal >= Fraction(str(spec["min_delta"]))
        guards = []
        for guard in spec["guards"]:
            actual = next((c["actual"] for c in checks if c["metric"] == guard["metric"] and c["arm"] == "candidate"), None)
            if "unit" in guard:
                baseline = next((c["actual"] for c in checks if c["metric"] == guard["metric"] and c["arm"] == "baseline"), None)
                guards.append(paired_guard(guard, baseline, actual))
                continue
            ok = number(actual) and (actual <= guard["value"] if guard["op"] == "le" else actual >= guard["value"])
            guards.append({**guard, "actual": actual, "passed": bool(ok)})
        benefit = {"metric": spec["metric"], "delta": delta,
                   "delta_exact": str(delta_decimal) if number(base) and number(cand) else None,
                   "min_delta": spec["min_delta"], "guards": guards,
                   "met": bool(reproduced and met and all(g["passed"] for g in guards))}
    return checks, reproduced, benefit


def interrupted_attempt(run):
    return {**run, "status": "INTERRUPTED", "reason": "Previous runner stopped without completion",
            "reproduced": False, "benefit": None}


def recover_attempts(app, job_id):
    """Called under the workspace writer lock, which has no other active runner."""
    for run in app.store.list("run", insertion_order=True):
        if run["job_id"] == job_id and run.get("status") == "RUNNING":
            name = run.get("active_container")
            if name:
                expected = {"p2p-" + run["id"] + "-" + arm for arm in ARMS}
                if name not in expected:
                    raise ValueError("Interrupted run has an invalid container identity")
                try:
                    remove_container(name)
                except CleanupPending as exc:
                    raise ValueError(str(exc)) from exc
            app.store.save("run", run["id"], {**interrupted_attempt(run), "recovered_at": now()})


def run_experiment(app, job_id, experiment_id):
    job = app.status(job_id)
    if job["stale"]:
        raise ValueError("Job is stale: " + ", ".join(job["stale"]) + "; create a new job from current memories")
    experiment = app.store.get("experiment", experiment_id)
    if experiment["job_id"] != job_id:
        raise ValueError("Experiment belongs to another job")
    if experiment.get("analysis") != job["analysis"]:
        raise ValueError("Experiment was registered under a different analysis; register it again")
    recover_attempts(app, job_id)
    plan = app.store.json(experiment["plan"])
    run = {"id": unique("run"), "experiment_id": experiment_id, "job_id": job_id, "started_at": now(),
           "plan": experiment["plan"], "source_tree": experiment["source_tree"], "input_tree": experiment["input_tree"]}
    app.store.save("run", run["id"], {**run, "status": "RUNNING", "arms": {}, "checks": []})
    try:
        try:
            if tree_hash(experiment["source_path"]) != experiment["source_tree"]:
                raise ValueError("Source tree changed since registration")
            if experiment["input_path"] and tree_hash(experiment["input_path"]) != run["input_tree"]:
                raise ValueError("Input tree changed since registration")
            run["image_id"] = image_id(plan["image"])
        except (ValueError, OSError) as exc:
            run.update(status="BLOCKED", reason=str(exc), arms={}, checks=[], reproduced=False, benefit=None)
            return app.store.save("run", run["id"], run)
        run_dir = app.store.root / "runs" / run["id"]
        run_dir.mkdir(mode=0o700)
        for arm in ARMS:  # separate fresh container per arm
            (run_dir / arm).mkdir(mode=0o700)
        arms = {}
        for arm in ARMS:
            if arm == "candidate":
                try:
                    if tree_hash(experiment["source_path"]) != run["source_tree"]:
                        raise ValueError("Source tree changed after baseline")
                    if experiment["input_path"] and tree_hash(experiment["input_path"]) != run["input_tree"]:
                        raise ValueError("Input tree changed after baseline")
                except (ValueError, OSError) as exc:
                    run.update(status="BLOCKED", reason=str(exc), arms=dict(arms), checks=[],
                               reproduced=False, benefit=None, finished_at=now())
                    return app.store.save("run", run["id"], run)
            record = run_arm(app, plan, run["image_id"], experiment, run_dir, arm)
            keep = record["result"]
            record["result"] = None if keep is None else app.store.put_json(keep)
            arms[arm] = record
            app.store.save("run", run["id"], {**run, "status": "RUNNING", "arms": dict(arms), "checks": []})
        sealed = app.store.put_json({"schema": 2, "run": run_identity(run),
                                     "arms": {arm: {k: rec[k] for k in SEALED} for arm, rec in arms.items()}})
        saved = app.store.save("run", run["id"], {**run, "arms": arms, "sealed": sealed, "finished_at": now(), "status": "FAIL"})
        verdict = evaluate(app, saved)
        return app.store.save("run", run["id"], {**saved, **{k: verdict[k] for k in ("checks", "reproduced", "benefit", "status")}})
    except CleanupPending as exc:
        pending = app.store.get("run", run["id"])
        app.store.save("run", run["id"], {**pending, "reason": str(exc)})
        raise ValueError(str(exc)) from exc
    except (KeyboardInterrupt, SystemExit):
        completed = app.store.get("run", run["id"]).get("arms", {})
        app.store.save("run", run["id"], {**run, "status": "INTERRUPTED", "reason": "Execution was interrupted",
                                         "arms": completed, "checks": [], "reproduced": False, "benefit": None,
                                         "finished_at": now()})
        raise
    except Exception as exc:  # the attempt must stay on record even when bookkeeping fails
        completed = app.store.get("run", run["id"]).get("arms", {})
        run.update(status="FAIL", reason=f"Run bookkeeping failed ({type(exc).__name__}): {exc}", arms=completed, checks=[], reproduced=False, benefit=None)
        return app.store.save("run", run["id"], run)


def stale_reason(experiment, run, app=None):
    try:
        if tree_hash(experiment["source_path"]) != run["source_tree"] or run["source_tree"] != experiment["source_tree"]:
            return "source changed after the run"
        if experiment["input_path"] and tree_hash(experiment["input_path"]) != run["input_tree"]:
            return "input changed after the run"
    except (ValueError, OSError) as exc:
        return str(exc)
    if app is not None and run.get("image_id"):
        try:
            current = image_id(app.store.json(experiment["plan"])["image"])
        except ValueError:
            return None
        if current != run["image_id"]:
            return "container image changed after the run"
    return None


def current_runs(app, experiment, *, historical=False):
    history = runs(app, experiment["id"])
    if not history:
        return history
    if any(run["status"] == "RUNNING" for run in history):
        try:
            with app.store.idle_writer():
                history = [interrupted_attempt(run) if run["status"] == "RUNNING" else run
                           for run in runs(app, experiment["id"])]
        except WorkspaceBusy:
            pass
    snapshot_reason = ("analysis changed after the experiment was registered" if historical
                       else stale_reason(experiment, experiment))
    current_image, unverified = None, None
    if not historical and any(run.get("image_id") for run in history):
        try:
            current_image = image_id(app.store.json(experiment["plan"])["image"])
        except ValueError as exc:
            unverified = str(exc)
    for index, run in enumerate(history):
        reason = snapshot_reason
        if not reason and current_image and run.get("image_id") and run["image_id"] != current_image:
            reason = "container image changed after the run"
        history[index] = {**run, "score_status": run["status"], "status": "STALE" if reason else run["status"],
                          "reason": reason or run.get("reason"), "image_unverified": unverified if run.get("image_id") else None}
    return history


def outcome_for_job(app, job, run_histories=None):
    rows = []
    analysis = app.store.json(job["analysis"]) if job["analysis"] else None
    registered = experiments(app, job["id"])
    for project in job["projects"]:
        pid = project["id"]
        project_experiments = [e for e in registered if e["project_id"] == pid]
        assessment = next((a for a in analysis["assessments"] if a["project_id"] == pid), None) if analysis else None
        scoped, scoped_support = [], set()
        if assessment and assessment["relevance"] == "findings_only":
            reviews = job.get("finding_reviews", {}).get(pid, {})
            for row in assessment["findings"]:
                key = reviews.get(row["id"])
                item_review = app.store.json(key) if key else None
                bindings = (test_binding(e) for e in project_experiments
                          if e.get("analysis") == job["analysis"] and e.get("finding_id") == row["id"])
                if item_review and item_review["evidence"] != evidence_key(
                        job["id"], project, job["analysis"], row["id"], bindings, job.get("analysis_author")):
                    item_review, key = None, None
                scoped_support.update(supported_tests(row, item_review))
                scoped.append({"id": row["id"], "target": row["target"], "proposed": row["proposed"],
                               "status": "STALE" if job["stale"] else finding_status(row, item_review), "review_hash": key,
                               "premise_review": item_review["premise"] if item_review else None,
                               "test_links": {eid: review["test_link"] for eid, review in
                                              (item_review or {}).get("test_reviews", {}).items()}})
        scoped_reviewed = bool(scoped) and all(item["status"].startswith("REVIEWED_") for item in scoped)
        key = job.get("reviews", {}).get(pid)
        review = app.store.json(key) if key else None
        listed, attempts, scope = [], [], []
        for experiment in project_experiments:
            active = experiment.get("analysis") == job["analysis"]
            history = (current_runs(app, experiment, historical=not active) if run_histories is None
                       else run_histories[experiment["id"]])
            run = history[-1] if history else None
            stale = None if active else "analysis changed after the experiment was registered"
            if active:
                scope.append({"id": experiment["id"], "plan_hash": experiment["plan"], "runs": history,
                              "source_tree": experiment["source_tree"], "input_tree": experiment["input_tree"],
                              "reference_sha256": experiment.get("reference_sha256")})
            status = "NOT_RUN" if run is None and not stale else "STALE" if run is None else ("STALE" if stale else run["status"])
            listed.append({"id": experiment["id"], "kind": experiment["kind"], "status": status, "runs": len(history), "current_analysis": active,
                           "run_id": run and run["id"], "reason": stale or (run or {}).get("reason"),
                           "image_unverified": (run or {}).get("image_unverified"),
                           "head_matches": bool(experiment.get("head_matches")),
                           "run_statuses": [item["status"] for item in history],
                           "finding_id": experiment.get("finding_id"), "oracle_sha256": experiment.get("oracle_sha256"),
                           "reference_sha256": experiment.get("reference_sha256")})
            if active:
                attempts.extend((experiment["kind"], item["status"], item) for item in history)
                if not history:
                    attempts.append((experiment["kind"], status, None))
        applicable_review = bool(review and review["analysis"] == job["analysis"] and
                                 review.get("evidence") == app.store.evidence_digest(pid, job["analysis"], scope, job_id=job["id"],
                                                                           research_snapshot=job["research"]["snapshot"],
                                                                           project_snapshot=project["snapshot"],
                                                                           author=job.get("analysis_author")))
        reviewed = bool(not job["stale"] and applicable_review and review["decision"] == "pass")
        statuses = {s for _, s, _ in attempts}
        if job["stale"]:
            decision, why = "STALE", "Project or research memory changed after this job was created; evidence applies only to the tested snapshots."
        elif assessment is None:
            decision, why = "NO_ANALYSIS", "No grounded analysis has been submitted."
        elif review and review["decision"] == "revise" and (applicable_review or (review.get("evidence") is None and review["analysis"] == job["analysis"])):
            decision, why = "ANALYSIS_REVIEW_REQUIRED", "A reviewer requested changes to the analysis for this project."
        elif assessment["relevance"] == "not_applicable" and not attempts:
            if reviewed:
                decision, why = "NOT_APPLICABLE", "The recorded project-specific review accepted the applicability rejection."
            else:
                decision, why = "APPLICABILITY_PENDING_REVIEW", "The analysis proposes not applicable; the decision needs project-specific review."
        elif not attempts and assessment["relevance"] == "findings_only":
            if scoped_reviewed:
                decision, why = "FINDINGS_REVIEWED", "Each finding has a recorded scoped review. Project benefit was not established."
            else:
                decision, why = "FINDINGS_PENDING_REVIEW", "One or more findings await evidence or scoped semantic review."
        elif assessment["relevance"] == "needs_evidence":
            decision, why = "NEEDS_PROJECT_EVIDENCE", "The assessment records missing evidence for the project. Resolve or review those gaps before claiming benefit."
        elif not attempts and listed:
            decision, why = "STALE_EVIDENCE", "Prior experiments are historical; register a plan for the current analysis."
        elif not attempts:
            decision, why = "ANALYSIS_ONLY", "Text analysis only; no experiment supports applicability, so this is not a verified recommendation."
        elif statuses == {"PASS"}:
            benefit_runs = [r for k, _, r in attempts if k == "project_benefit"]
            met = bool(benefit_runs) and all(r["benefit"]["met"] for r in benefit_runs)
            benefit_ids = {r["experiment_id"] for r in benefit_runs}
            attested = reviewed and benefit_ids <= set(review.get("verified_experiments", []))
            if not met:
                decision, why = "REPRODUCED_BENEFIT_UNPROVEN", "The source reproduced as declared, but not every project_benefit experiment met its declared criteria (or none exists). Reproduction does not prove utility."
            elif assessment["relevance"] == "findings_only" and not (benefit_ids <= scoped_support):
                decision = "FINDINGS_REVIEWED" if scoped_reviewed else "FINDINGS_PENDING_REVIEW"
                why = "Each passing benefit experiment requires a passing scoped premise and relevant test-link review before project benefit can be established."
            elif assessment["relevance"] == "not_applicable":
                decision, why = "APPLICABILITY_CONFLICT", "The declared benefit passed and conflicts with the analysis rejection; revise the assessment before drawing a conclusion."
            elif attested:
                decision, why = "BENEFIT_DEMONSTRATED", "The benefit met its declared criteria and the reviewer attested to the method, target data and independent reference for every benefit experiment."
            elif reviewed:
                decision, why = "BENEFIT_CRITERIA_MET", "The measurements met the declared thresholds. A reviewer must verify each benefit experiment's method, target data and frozen independent reference to establish benefit."
            else:
                decision, why = "BENEFIT_PENDING_REVIEW", "The benefit criteria were met on the declared inputs. A passing review must cover this project's analysis, plans and recorded runs."
        elif "PASS" in statuses:
            decision, why = "MIXED_EVIDENCE", "Some experiments passed while others failed, are stale, blocked or not run; no clean conclusion is possible."
        elif "STALE" in statuses:
            decision, why = "STALE_EVIDENCE", "Source or inputs changed after the run; re-run the experiment."
        elif "FAIL" in statuses:
            decision, why = "EXPERIMENT_FAILED", "At least one experiment ran and failed its declared checks."
        elif "INTERRUPTED" in statuses or "RUNNING" in statuses:
            decision, why = "EXPERIMENT_INCOMPLETE", "An experiment attempt started without a completed comparison."
        elif "BLOCKED" in statuses:
            decision, why = "EXPERIMENT_BLOCKED", "Execution was blocked (missing runtime, image or changed inputs); see the run reason."
        else:
            decision, why = "EXPERIMENT_NOT_RUN", "Experiments are registered but have not been run."
        result = {"project_id": pid, "decision": decision, "explanation": why, "experiments": listed,
                  "relevance": assessment["relevance"] if assessment else None, "review_current": reviewed}
        if scoped:
            for item in scoped:
                item["experiments"] = [x["id"] for x in listed if x["current_analysis"] and x["finding_id"] == item["id"]]
            result["findings"] = scoped
        rows.append(result)
    return rows
