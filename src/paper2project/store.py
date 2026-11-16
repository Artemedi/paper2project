"""Immutable content snapshots and transactional local workspace metadata."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
try:
    import fcntl
except ImportError:
    fcntl = None
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import threading
import uuid

ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}\Z")


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError("Identifier must contain 1..80 lowercase letters, digits, underscores or hyphens")
    return value


def slug(value):
    result = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:60]
    suffix = hashlib.sha256(value.encode()).hexdigest()[:8]
    if re.search(r"[^\W_]", re.sub(r"[a-z0-9]", "", value.lower())):  # letters/digits outside a-z0-9 were dropped: keep names distinct
        result = result[:50].strip("-")
        return identifier((result + "-" if result else "item-") + suffix)
    return identifier(result or "item-" + hashlib.sha256(value.encode()).hexdigest()[:12])


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def _finite(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("Nonfinite JSON value: " + text)
    return value


def loads(value):
    try:
        return json.loads(value, parse_float=_finite,
                          parse_constant=lambda x: (_ for _ in ()).throw(ValueError("Nonfinite JSON value: " + x)))
    except RecursionError as exc:
        raise ValueError("JSON nesting is too deep") from exc


def digest(value):
    return hashlib.sha256(value).hexdigest()


def unique(prefix):
    return prefix + "-" + uuid.uuid4().hex[:16]


@contextmanager
def regular_file(path):
    """Open one regular file through non-symlink directory descriptors."""
    parts = Path(path).absolute().parts
    directory = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY)
    file_fd = None
    try:
        for part in parts[1:-1]:
            next_dir = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = next_dir
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise ValueError("Input is not a regular file")
        with os.fdopen(file_fd, "rb") as stream:
            file_fd = None
            yield stream
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory)


class WorkspaceBusy(ValueError):
    """Another writer currently owns the workspace."""


class _WorkspaceLock:
    """One flock per canonical workspace and process; re-entrant for the owning thread only, fail-fast for everyone else."""

    def __init__(self, path):
        self.path = path
        self.mutex = threading.Lock()
        self.owner = None
        self.depth = 0
        self.fd = None

    def acquire(self):
        me = threading.get_ident()
        if self.owner == me:
            self.depth += 1
            return
        if not self.mutex.acquire(blocking=False):
            raise WorkspaceBusy("workspace is busy: another writer holds the lock")
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise WorkspaceBusy("workspace is busy: another writer holds the lock") from None
            except BaseException:
                os.close(fd)
                raise
        except BaseException:
            self.mutex.release()
            raise
        self.fd, self.owner, self.depth = fd, me, 1

    def release(self):
        self.depth -= 1
        if self.depth:
            return
        fd, self.fd, self.owner = self.fd, None, None
        try:
            os.close(fd)  # closing the descriptor drops the flock
        finally:
            self.mutex.release()


_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _forget_locks_in_child():
    """A forked child shares the parent's descriptor but must not own it: reset every existing lock object in place
    (existing Store objects keep working), close the inherited copy of the descriptor (the parent's flock stays)."""
    global _LOCKS_GUARD
    _LOCKS_GUARD = threading.Lock()
    for lock in list(_LOCKS.values()):
        if lock.fd is not None:
            try:
                os.close(lock.fd)
            except OSError:
                pass
        lock.mutex, lock.owner, lock.depth, lock.fd = threading.Lock(), None, 0, None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_forget_locks_in_child)


class Store:
    def __init__(self, root):
        if fcntl is None:
            raise ValueError("Paper2Project requires a Unix filesystem lock. On Windows, run it in WSL with a Linux workspace.")
        self.root = Path(root).absolute()
        if self.root.is_symlink():
            raise ValueError("Workspace must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = self.root.resolve()
        for name in ("objects", "runs", "sources", "environments"):
            path = self.root / name
            if path.is_symlink():
                raise ValueError("Workspace directories must not be symlinks")
            path.mkdir(exist_ok=True)
        database = self.root / "state.sqlite3"
        if database.is_symlink():
            raise ValueError("Database must not be a symlink")
        self.database = database
        lock_path = self.root / ".lock"
        if lock_path.is_symlink():
            raise ValueError("Lock file must not be a symlink")
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(str(self.root), _WorkspaceLock(lock_path))
        with self.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT NOT NULL,id TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(kind,id))")

    @contextmanager
    def writer(self):
        """Exclusive workspace writer for a whole read-modify-write operation (local filesystem only)."""
        self._lock.acquire()
        try:
            yield
        finally:
            self._lock.release()

    @contextmanager
    def idle_writer(self):
        """Hold the writer lock only if no writer, including this thread, is active."""
        if self._lock.owner == threading.get_ident():
            raise WorkspaceBusy("workspace is busy: current thread holds the lock")
        with self.writer():
            yield

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.database, timeout=30)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def put(self, payload):
        if not isinstance(payload, bytes):
            raise TypeError("Object payload must be bytes")
        key = digest(payload)
        path = self.root / "objects" / key
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)  # publish the object name before committing its metadata reference
                finally:
                    os.close(directory)
            except FileExistsError:
                if self.blob(key) != payload:
                    raise ValueError("Content-addressed object collision")
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
        return key

    def blob(self, key):
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("Invalid object hash")
        path = self.root / "objects" / key
        if path.is_symlink() or not path.is_file():
            raise ValueError("Missing or unsafe object: " + key)
        value = path.read_bytes()
        if digest(value) != key:
            raise ValueError("Object integrity check failed: " + key)
        return value

    def put_json(self, value):
        return self.put(dumps(value).encode())

    def json(self, key):
        return loads(self.blob(key))

    @staticmethod
    def evidence_digest(project_id, analysis, plans, *, job_id, research_snapshot, project_snapshot, author):
        scope = {"schema": 2, "job_id": job_id, "research_snapshot": research_snapshot,
                 "project_snapshot": project_snapshot, "author": author,
                 "project_id": project_id, "analysis": analysis, "experiments": [
            {"id": entry["id"], "plan": entry["plan_hash"], "source_tree": entry["source_tree"],
             "input_tree": entry["input_tree"],
             **({"reference_sha256": entry["reference_sha256"]} if entry.get("reference_sha256") else {}),
             "runs": [[run["id"], run["status"], run.get("sealed")] for run in entry["runs"]]}
            for entry in plans]}
        return digest(dumps(scope).encode())

    def save(self, kind, key, value):
        identifier(kind)
        identifier(key)
        with self.db() as db:
            db.execute("INSERT INTO records(kind,id,body) VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body", (kind,key,dumps(value)))
        return value

    def get(self, kind, key):
        identifier(kind)
        identifier(key)
        with self.db() as db:
            row = db.execute("SELECT body FROM records WHERE kind=? AND id=?", (kind,key)).fetchone()
        if row is None:
            raise ValueError(f"Unknown {kind}: {key}")
        return loads(row[0])

    def list(self, kind, *, insertion_order=False):
        identifier(kind)
        with self.db() as db:
            order = "rowid" if insertion_order else "id"
            rows = db.execute(f"SELECT id,body FROM records WHERE kind=? ORDER BY {order}", (kind,)).fetchall()
        records = []
        for key, body in rows:
            record = loads(body)
            if kind == "run" and record.get("id") != key:
                raise ValueError("Run record index disagrees with its body: " + key)
            records.append(record)
        return records
