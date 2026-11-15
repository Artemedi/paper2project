"""Source-bound container experiments scored by a host-owned comparator.

Source code runs only inside an offline, unprivileged, resource-limited Podman
container with its source and inputs read-only. The plan, scorer and workspace
state never enter the container, so the experiment cannot rewrite its own verdict.
"""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import signal
import stat
import subprocess
import time

from .store import digest, dumps, loads, now, unique

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
    if limit is not None and path.stat().st_size > limit:
        raise ValueError(f"File larger than {limit} bytes: {path.name}")
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest()


def tree_hash(path):
    """Deterministic hash of everything the container can see: names, modes, content, .git included."""
    root = Path(path)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Experiment tree must be a real directory: " + str(path))
    entries, total = [], 0
    for item in sorted(root.rglob("*")):
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


def copy_tree(source, destination, expected):
    """Copy a hashed tree into the workspace and prove the copy has exactly the hash that was registered.

    The container later mounts only the copy, so the live directory can no longer change what runs after it was hashed.
    Files are opened without following symlinks; modes are reproduced because they are part of the hash."""
    source, destination = Path(source), Path(destination)
    destination.mkdir(parents=True, mode=0o700)
    directories = []
    copied = 0
    try:
        for item in sorted(source.rglob("*")):
            relative = item.relative_to(source)
            target = destination / relative
            mode = item.lstat().st_mode
            if stat.S_ISDIR(mode):
                target.mkdir(mode=0o700)
                directories.append((target, mode & 0o7777))
            elif stat.S_ISREG(mode):
                fd = os.open(item, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)  # never block on a swapped-in FIFO
                with os.fdopen(fd, "rb") as read, open(target, "xb") as write:
                    if not stat.S_ISREG(os.fstat(read.fileno()).st_mode):
                        raise ValueError("Experiment tree entry is not a regular file: " + relative.as_posix())
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
        for target, _ in directories:
            os.chmod(target, 0o700)
        os.chmod(destination, 0o700)
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return destination


def git_head(source):
    """Commit a checkout claims to be at, read without running git; None if unknowable."""
    git = Path(source) / ".git"
    if git.is_symlink() or not git.is_dir():
        return None
    root = git.resolve()

    def read(path, limit=4096):
        """Only regular files that stay inside .git; content is never trusted or echoed."""
        resolved = path.resolve()
        if path.is_symlink() or root not in resolved.parents or not resolved.is_file() or resolved.stat().st_size > limit:
            return None
        return resolved.read_text()

    try:
        head = (read(git / "HEAD") or "").strip()
        found = None
        if re.fullmatch(r"[0-9a-f]{40}", head):
            found = head
        elif head.startswith("ref: refs/") and ".." not in head:
            ref = head[5:]
            value = read(git / ref)
            if value is not None:
                found = value.strip()
            else:
                for line in (read(git / "packed-refs", 8 << 20) or "").splitlines():
                    if line.endswith(" " + ref):
                        found = line.split()[0]
        return found if found and re.fullmatch(r"[0-9a-f]{40}", found) else None
    except (OSError, UnicodeError):
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
    numbers = (isinstance(actual, (int, float)) and not isinstance(actual, bool)
               and isinstance(expected, (int, float)) and not isinstance(expected, bool))
    if not numbers or not number(actual) or not number(expected):
        return False
    try:
        bound = tolerance if comparison == "abs" else tolerance * abs(expected)
        spread = abs(actual - expected)
        return math.isfinite(bound) and math.isfinite(spread) and spread <= bound  # inf <= inf must not pass
    except OverflowError:
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
    delta = candidate - base if guard["direction"] == "higher_is_better" else base - candidate
    try:
        if unit == "speedup_ratio":
            if base <= 0 or candidate <= 0:
                return {**result, "reason": "NON_POSITIVE_SPEEDUP_METRIC"}
            ratio = base / candidate
            if not number(ratio):
                return {**result, "reason": "NON_FINITE_COMPARISON"}
            return {**result, "ratio": ratio, "passed": ratio >= guard["min_ratio"]}
        if unit == "relative_fraction" and base == 0:
            if guard.get("zero_baseline_policy") == "require_non_worse":
                return {**result, "reason": "ZERO_BASE_NON_WORSE", "passed": delta >= 0}
            return {**result, "reason": "ZERO_BASELINE_FOR_RELATIVE_METRIC"}
        change = delta if unit == "absolute" else delta / abs(base)
    except OverflowError:
        return {**result, "reason": "NON_FINITE_COMPARISON"}
    if not number(change):
        return {**result, "reason": "NON_FINITE_COMPARISON"}
    passed = ("min_gain" not in guard or change >= guard["min_gain"]) and (
        "max_regression" not in guard or -change <= guard["max_regression"])
    return {**result, "change": change, "passed": passed}


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
            or not re.fullmatch(r"[0-9a-f]{40}", str(source.get("revision", ""))) \
            or not isinstance(source.get("license"), str) or not source["license"].strip():
        raise ValueError("source needs path, 40-hex revision and license")
    if plan.get("input") is not None and (not isinstance(plan["input"], dict) or not isinstance(plan["input"].get("path"), str)):
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
        slack = 0.0
        if target.get("comparison", "exact") != "exact":  # a loose tolerance would let any number "match" the expectation
            scale = 1 if target["comparison"] == "abs" else max(abs(target["expected"]["baseline"]), abs(target["expected"]["candidate"]))
            slack = target["tolerance"] * scale
        if 2 * slack >= benefit["min_delta"]:
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


def add_experiment(app, job_id, plan):
    job = app.store.get("job", job_id)
    if not job["analysis"]:
        raise ValueError("Submit a grounded analysis before registering experiments")
    plan = validate_plan(plan, {p["id"] for p in job["projects"]})
    assessment = next(a for a in app.store.json(job["analysis"])["assessments"] if a["project_id"] == plan["project_id"])
    if assessment["relevance"] == "not_applicable":
        raise ValueError("Analysis marked this project not_applicable; revise the analysis first")
    source = Path(app.local(plan["source"]["path"]))
    inputs = Path(app.local(plan["input"]["path"])) if plan.get("input") else None
    mount_safe(source)
    if inputs:
        mount_safe(inputs)
    source_tree = tree_hash(source)  # rejects symlinks and special files before anything inside is read
    input_tree = tree_hash(inputs) if inputs else None
    if plan["kind"] == "project_benefit" and not input_tree["bytes"]:
        raise ValueError("project_benefit needs non-empty target data in the input directory")
    head = git_head(source)
    if head is not None and head != plan["source"]["revision"]:
        raise ValueError(f"Source checkout is at {head}, not the declared revision {plan['source']['revision']}")
    experiment_id = unique("exp")
    snapshot_dir = app.store.root / "snapshots" / experiment_id
    source = copy_tree(source, snapshot_dir / "source", source_tree)
    inputs = copy_tree(inputs, snapshot_dir / "input", input_tree) if inputs else None
    entry = {"id": experiment_id, "job_id": job_id, "project_id": plan["project_id"], "kind": plan["kind"],
             "plan": app.store.put_json(plan), "created_at": now(), "head_matches": head is not None, "analysis": job["analysis"],
             "source_tree": source_tree, "input_tree": input_tree,
             "source_path": str(source.resolve()), "input_path": str(inputs.resolve()) if inputs else None,
             "origin_source_path": plan["source"]["path"], "origin_input_path": plan["input"]["path"] if plan.get("input") else None}
    app.store.save("experiment", entry["id"], entry)
    app.event(job_id, "experiment", {"experiment": entry["id"], "plan": entry["plan"]})
    return entry


def experiments(app, job_id):
    return [e for e in app.store.list("experiment") if e["job_id"] == job_id]


SEALED = ("exit_code", "timed_out", "error", "result", "result_sha256")


def evaluate(app, run):
    """Re-derive the verdict from the plan and stored result objects; the run row's own status is not trusted."""
    arms = run.get("arms") or {}
    if not arms:
        return {**run, "status": "BLOCKED" if run.get("status") == "BLOCKED" else "FAIL"}
    if not run.get("sealed"):  # facts that decide the verdict must come from a hashed object, not from an editable row
        return {**run, "status": "FAIL", "reproduced": False, "benefit": None, "reason": "run has no sealed arm facts"}
    facts = app.store.json(run["sealed"])  # rehashed on read
    if set(facts) != set(arms):
        return {**run, "status": "FAIL", "reproduced": False, "benefit": None, "reason": "sealed arm facts disagree with the run row"}
    plan = app.store.json(run["plan"])
    loaded = {arm: {**rec, **facts[arm], "result": app.store.json(facts[arm]["result"]) if facts[arm].get("result") else None} for arm, rec in arms.items()}
    checks, reproduced, benefit = score(plan, loaded)
    return {**run, "checks": checks, "reproduced": reproduced, "benefit": benefit, "status": "PASS" if reproduced else "FAIL"}


def runs(app, experiment_id):
    rows = sorted((r for r in app.store.list("run") if r["experiment_id"] == experiment_id), key=lambda r: r["started_at"])
    return [evaluate(app, r) for r in rows]


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
    """Hash and parse everything the container wrote; the container is hostile, so failures become records."""
    record["stdout"], record["stderr"] = read_log(stdout), read_log(stderr)
    record["stdout_sha256"], record["stderr_sha256"] = file_digest(stdout), file_digest(stderr)
    files, entries = {}, 0
    for item in out.rglob("*"):
        entries += 1
        relative = item.relative_to(out).as_posix()
        if entries > MAX_OUTPUT_ENTRIES:
            record["error"] = "Output has more than 1000 entries"
            break
        if item.is_symlink():
            record["error"] = "Output contains a symlink: " + relative
        elif item.is_file():
            try:
                files[relative] = file_digest(item, MAX_OUTPUT_FILE)
            except ValueError:
                files[relative] = None
                record["error"] = f"Output file {relative} exceeds {MAX_OUTPUT_FILE} bytes"
    record["output_files"] = files
    result_path = out / "result.json"
    if record["exit_code"] == 0 and not record["error"]:
        if result_path.is_symlink() or not result_path.is_file() or result_path.stat().st_size > MAX_RESULT:
            raise ValueError("result.json missing, unsafe or larger than 1 MiB")
        raw = result_path.read_bytes()
        record["result"], record["result_sha256"] = loads(raw), digest(raw)


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


def deleted_mapping(pids, device):
    """True if a container process maps a file that was unlinked on the output filesystem. Its size cannot be read from the host
    (map_files is not accessible), so the mapping is treated as a breach rather than counted."""
    want = f"{os.major(device):02x}:{os.minor(device):02x}"
    for pid in pids:
        try:
            with open(f"/proc/{pid}/maps") as handle:
                for line in handle:
                    fields = line.split(None, 5)
                    if len(fields) == 6 and fields[3] == want and fields[5].rstrip().endswith("(deleted)"):
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
    stack = [str(out)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for item in it:
                    entries += 1
                    if entries > MAX_OUTPUT_ENTRIES:
                        return f"more than {MAX_OUTPUT_ENTRIES} output entries"
                    info = item.stat(follow_symlinks=False)
                    if item.is_dir(follow_symlinks=False):
                        stack.append(item.path)
                    elif (info.st_dev, info.st_ino) not in seen:
                        seen.add((info.st_dev, info.st_ino))
                        total += max(info.st_size, info.st_blocks * 512)  # logical or allocated, whichever is larger (sparse files)
        except FileNotFoundError:
            continue
        if total > MAX_OUTPUT_TOTAL:
            return f"output directory larger than {MAX_OUTPUT_TOTAL} bytes"
    if pids:
        device = os.stat(out).st_dev
        if deleted_mapping(pids, device):
            return "an unlinked file is kept alive by a memory mapping"
        if total + deleted_open_bytes(pids, device) > MAX_OUTPUT_TOTAL:
            return f"output (including unlinked open files) larger than {MAX_OUTPUT_TOTAL} bytes"
    return None


def stop_container(proc, name):
    """Kill by name first (killing the podman client does not stop the container), remove it, then end the client."""
    for verb in (["kill", name], ["rm", "-f", name]):
        try:
            subprocess.run([*podman_command(), *verb], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()


def run_arm(app, plan, image, experiment, run_dir, arm):
    out = run_dir / arm / "output"
    out.mkdir(parents=True, mode=0o700)
    name = unique("p2p-" + arm)
    argv = container_argv(plan["limits"], image, plan["arms"][arm], experiment["source_path"], experiment["input_path"], out, name)
    stdout, stderr = run_dir / arm / "stdout.txt", run_dir / arm / "stderr.txt"
    started = time.monotonic()
    record = {"arm": arm, "exit_code": None, "timed_out": False, "error": None, "result": None, "result_sha256": None,
              "output_files": {}, "stdout": "", "stderr": "", "stdout_sha256": None, "stderr_sha256": None}
    try:
        with stdout.open("wb") as o, stderr.open("wb") as e:
            proc = subprocess.Popen(argv, stdout=o, stderr=e, stdin=subprocess.DEVNULL, start_new_session=True)
            deadline = time.monotonic() + plan["limits"]["timeout"] + 30  # the container has its own --timeout; the extra time is cleanup
            root_pid = None
            try:
                while True:
                    try:
                        record["exit_code"] = proc.wait(timeout=POLL_SECONDS)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                    try:
                        root_pid = root_pid or container_root_pid(name)
                        breach = output_breach(stdout, stderr, out, process_tree(root_pid) if root_pid else ())
                    except OSError as exc:  # an output tree the watchdog cannot read is not an output tree it can ignore
                        breach = "output directory cannot be inspected (" + type(exc).__name__ + ")"
                    if breach or time.monotonic() > deadline:
                        stop_container(proc, name)
                        record["exit_code"] = proc.returncode
                        if breach:
                            record["error"] = "output limit exceeded: " + breach
                        else:
                            record["timed_out"] = True
                        break
            except BaseException:  # KeyboardInterrupt and the like must not leave a running container without a watchdog
                stop_container(proc, name)
                raise
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
            delta = (cand - base) if spec["direction"] == "increase" else (base - cand)
            delta = delta if number(delta) else None
            met = delta is not None and delta >= spec["min_delta"]
        guards = []
        for guard in spec["guards"]:
            actual = next((c["actual"] for c in checks if c["metric"] == guard["metric"] and c["arm"] == "candidate"), None)
            if "unit" in guard:
                baseline = next((c["actual"] for c in checks if c["metric"] == guard["metric"] and c["arm"] == "baseline"), None)
                guards.append(paired_guard(guard, baseline, actual))
                continue
            ok = number(actual) and (actual <= guard["value"] if guard["op"] == "le" else actual >= guard["value"])
            guards.append({**guard, "actual": actual, "passed": bool(ok)})
        benefit = {"metric": spec["metric"], "delta": delta, "min_delta": spec["min_delta"], "guards": guards,
                   "met": bool(reproduced and met and all(g["passed"] for g in guards))}
    return checks, reproduced, benefit


def run_experiment(app, job_id, experiment_id):
    job = app.status(job_id)
    if job["stale"]:
        raise ValueError("Job is stale: " + ", ".join(job["stale"]) + "; create a new job from current memories")
    experiment = app.store.get("experiment", experiment_id)
    if experiment["job_id"] != job_id:
        raise ValueError("Experiment belongs to another job")
    if experiment.get("analysis") != job["analysis"]:
        raise ValueError("Experiment was registered under a different analysis; register it again")
    plan = app.store.json(experiment["plan"])
    run = {"id": unique("run"), "experiment_id": experiment_id, "job_id": job_id, "started_at": now(),
           "plan": experiment["plan"], "source_tree": experiment["source_tree"], "input_tree": experiment["input_tree"]}
    try:
        if tree_hash(experiment["source_path"]) != experiment["source_tree"]:
            raise ValueError("Source tree changed since registration")
        if experiment["input_path"] and tree_hash(experiment["input_path"]) != experiment["input_tree"]:
            raise ValueError("Input tree changed since registration")
        run["image_id"] = image_id(plan["image"])
    except (ValueError, OSError) as exc:
        run.update(status="BLOCKED", reason=str(exc), arms={}, checks=[], reproduced=False, benefit=None)
        return app.store.save("run", run["id"], run)
    try:
        run_dir = app.store.root / "runs" / run["id"]
        run_dir.mkdir(mode=0o700)
        for arm in ARMS:  # separate fresh container per arm
            (run_dir / arm).mkdir(mode=0o700)
        arms = {arm: run_arm(app, plan, run["image_id"], experiment, run_dir, arm) for arm in ARMS}
        for record in arms.values():
            keep = record["result"]
            record["result"] = None if keep is None else app.store.put_json(keep)
        sealed = app.store.put_json({arm: {k: rec[k] for k in SEALED} for arm, rec in arms.items()})
        saved = app.store.save("run", run["id"], {**run, "arms": arms, "sealed": sealed, "finished_at": now(), "status": "FAIL"})
        verdict = evaluate(app, saved)
        return app.store.save("run", run["id"], {**saved, **{k: verdict[k] for k in ("checks", "reproduced", "benefit", "status")}})
    except Exception as exc:  # the attempt must stay on record even when bookkeeping fails
        run.update(status="FAIL", reason=f"Run bookkeeping failed ({type(exc).__name__}): {exc}", arms={}, checks=[], reproduced=False, benefit=None)
        return app.store.save("run", run["id"], run)


def stale_reason(experiment, run, app=None):
    try:
        if tree_hash(experiment["source_path"]) != run["source_tree"] or run["source_tree"] != experiment["source_tree"]:
            return "source changed after the run"
        if experiment["input_path"] and tree_hash(experiment["input_path"]) != run["input_tree"]:
            return "input changed after the run"
    except (ValueError, OSError) as exc:
        return str(exc)
    if app is not None and run.get("image_id"):  # evidence belongs to the exact image that produced it
        try:
            current = image_id(app.store.json(experiment["plan"])["image"])
        except ValueError:
            return None  # cannot be checked here (no runtime/image): reported by image_unverified, not treated as a change
        if current != run["image_id"]:
            return "container image changed after the run"
    return None


def image_unverified(experiment, run, app):
    """Why the image identity of a stored run could not be re-checked on this host, or None."""
    if run and run.get("image_id"):
        try:
            image_id(app.store.json(experiment["plan"])["image"])
        except ValueError as exc:
            return str(exc)
    return None


def outcome_for_job(app, job):
    rows = []
    analysis = app.store.json(job["analysis"]) if job["analysis"] else None
    review = app.store.json(job["review"]) if job["review"] else None
    registered = {e["id"] for e in experiments(app, job["id"])}
    # the review covers the analysis it names and the experiment plans that existed when it was recorded
    reviewed = bool(review) and review["decision"] == "pass" and review["analysis"] == job["analysis"] \
        and registered <= set(review.get("experiments", []))
    for project in job["projects"]:
        pid = project["id"]
        assessment = next((a for a in analysis["assessments"] if a["project_id"] == pid), None) if analysis else None
        listed, latest = [], []
        for experiment in (e for e in experiments(app, job["id"]) if e["project_id"] == pid):
            history = runs(app, experiment["id"])
            run = history[-1] if history else None
            stale = stale_reason(experiment, run, app) if run else None
            if experiment.get("analysis") != job["analysis"]:
                stale = "analysis changed after the experiment was registered"
            status = "NOT_RUN" if run is None and not stale else "STALE" if run is None else ("STALE" if stale else run["status"])
            listed.append({"id": experiment["id"], "kind": experiment["kind"], "status": status, "runs": len(history),
                           "run_id": run and run["id"], "reason": stale or (run or {}).get("reason"),
                           "image_unverified": image_unverified(experiment, run, app)})
            latest.append((experiment["kind"], status, run))
        statuses = {s for _, s, _ in latest}
        if job["stale"]:
            decision, why = "STALE", "Project or research memory changed after this job was created; evidence applies only to the tested snapshots."
        elif assessment is None:
            decision, why = "NO_ANALYSIS", "No grounded analysis has been submitted."
        elif assessment["relevance"] == "not_applicable":
            decision, why = "NOT_APPLICABLE", "Analysis found the research not applicable to this project."
        elif not listed:
            decision, why = "ANALYSIS_ONLY", "Text analysis only; no experiment supports applicability, so this is not a verified recommendation."
        elif statuses == {"PASS"}:
            benefit_runs = [r for k, _, r in latest if k == "project_benefit"]
            met = bool(benefit_runs) and all(r["benefit"]["met"] for r in benefit_runs)
            if not met:
                decision, why = "REPRODUCED_BENEFIT_UNPROVEN", "The source reproduced as declared, but not every project_benefit experiment met its declared criteria (or none exists). Reproduction does not prove utility."
            elif reviewed:
                decision, why = "BENEFIT_DEMONSTRATED", "Every experiment passed, an independently checked project_benefit experiment met its declared criteria on the declared inputs, and the analysis review passed."
            else:
                decision, why = "BENEFIT_PENDING_REVIEW", "The benefit criteria were met on the declared inputs, but the analysis has no passing independent review that covers the analysis and every registered experiment plan (review again after adding experiments)."
        elif "PASS" in statuses:
            decision, why = "MIXED_EVIDENCE", "Some experiments passed while others failed, are stale, blocked or not run; no clean conclusion is possible."
        elif "STALE" in statuses:
            decision, why = "STALE_EVIDENCE", "Source or inputs changed after the run; re-run the experiment."
        elif "FAIL" in statuses:
            decision, why = "EXPERIMENT_FAILED", "At least one experiment ran and failed its declared checks."
        elif "BLOCKED" in statuses:
            decision, why = "EXPERIMENT_BLOCKED", "Execution was blocked (missing runtime, image or changed inputs); see the run reason."
        else:
            decision, why = "EXPERIMENT_NOT_RUN", "Experiments are registered but have not been run."
        rows.append({"project_id": pid, "decision": decision, "explanation": why, "experiments": listed})
    return rows
