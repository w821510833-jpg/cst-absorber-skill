"""Bounded controller for explicitly prepared cases, with injected workers only.

No solver or process-management API is imported. A worker may provide
ownership(attempt_dir) -> (recorded_identity, observed_identity) and
cleanup_identity(recorded_identity) -> {owned_closed: True}. Both callbacks
are injected; the controller checks identity before invoking an action.
Unconfirmed closure leaves a durable lock and prevents automatic recovery.
The worker must read its immutable runtime.json and apply max_cpus/max_modes
to the backend. The 0.2 second closure wait bounds this offline controller's
confirmation window; it is not a suitable shutdown deadline for every solver.
Native callers must keep a RunSupervisor alive and wait for verified teardown
before allowing their interpreter to exit. The CLI does this automatically.
"""
from __future__ import annotations

import copy
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import threading
import time
from typing import Callable
import uuid


class RuntimeSafetyError(ValueError):
    """An invalid prepared plan must never reach the worker."""


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _check_assets(case):
    """Recheck current STL bytes against the prepared region-id audit hashes.

    Generic controller-only fixtures need no physical geometry. A declared STL
    does require the full asset identity. Workers must still stage and verify
    their own immutable assets to close the check-to-use race at their boundary.
    """
    geometry = case.get("geometry")
    if not isinstance(geometry, dict):
        return
    regions = geometry.get("regions", [])
    if not isinstance(regions, list):
        raise RuntimeSafetyError("geometry regions must be an explicit list")
    stl_regions = [region for region in regions if isinstance(region, dict) and region.get("kind") == "stl"]
    if not stl_regions:
        return
    audit = case.get("geometry_audit")
    hashes = audit.get("asset_hashes") if isinstance(audit, dict) else None
    if not isinstance(hashes, dict):
        raise RuntimeSafetyError("STL requires geometry_audit.asset_hashes")
    seen = set()
    for region in stl_regions:
        identifier = region.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise RuntimeSafetyError("STL region identity missing or duplicated")
        seen.add(identifier)
        expected = hashes.get(identifier)
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise RuntimeSafetyError("STL audited asset hash missing or invalid: " + identifier)
        filename = region.get("path")
        if not isinstance(filename, str) or not filename:
            raise RuntimeSafetyError("STL source requires an absolute asset path")
        asset = Path(filename)
        if not asset.is_absolute() or asset.is_symlink() or not asset.is_file():
            raise RuntimeSafetyError("STL source missing or not an absolute regular asset: " + identifier)
        if _file_hash(asset) != expected:
            raise RuntimeSafetyError("STL source content changed: " + identifier)


def _write(path, value, exclusive=False):
    if exclusive:
        with path.open("xb") as target:
            target.write(_bytes(value))
            target.flush()
            os.fsync(target.fileno())
        return
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        _write(temporary, value, exclusive=True)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read(path):
    if path.is_symlink():
        raise RuntimeSafetyError("control file symlink is not owned")
    return json.loads(path.read_text(encoding="utf-8"))


def _within(path, root):
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _artifact(root, name):
    if not isinstance(name, str) or not name or "\\" in name:
        raise RuntimeSafetyError("artifact must be a relative POSIX path")
    relative = Path(name)
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        raise RuntimeSafetyError("artifact escapes owned attempt")
    path = root / relative
    if not _within(path, root) or any(p.is_symlink() for p in (path, *path.parents) if _within(p, root)):
        raise RuntimeSafetyError("artifact symlink is not owned")
    if not path.is_file():
        raise RuntimeSafetyError("artifact is missing")
    return path


def request_pause(root: Path) -> None:
    root = Path(root)
    if root.is_symlink():
        raise RuntimeSafetyError("run root symlink is not owned")
    root.mkdir(parents=True, exist_ok=True)
    pause = root / "pause.json"
    _pause_snapshot(pause)  # Validate existing and dangling control nodes before writing.
    _write(pause, {"schema_version": "1.0", "pause": True})


def _pause_snapshot(path):
    """Bind a valid pause request to its actual file, before cache validation."""
    path = Path(path)
    if path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)()):
        raise RuntimeSafetyError("pause control link is not owned")
    try:
        with path.open("rb") as source:
            identity = os.fstat(source.fileno())
            content = source.read(65537)
    except FileNotFoundError:
        return None
    if len(content) > 65536:
        raise RuntimeSafetyError("pause control exceeds size bound")
    marker = json.loads(content.decode("utf-8"))
    if (not isinstance(marker, dict) or set(marker) != {"schema_version", "pause"}
            or marker["schema_version"] != "1.0" or marker["pause"] is not True):
        raise RuntimeSafetyError("corrupted pause control")
    return {"marker": marker, "identity": (identity.st_dev, identity.st_ino,
                                            identity.st_mtime_ns, identity.st_size)}


def _paused(root):
    return _pause_snapshot(Path(root) / "pause.json") is not None


def _consume_pause(root, observed, deadline):
    """Claim only the observed request after ownership/cache validation.

    An atomic rename separates the consumed file from concurrent new requests.
    A renewed file encountered during validation is restored without replacing
    a still newer request. Only this controller's private claimed file is removed.
    """
    if observed is None:
        return True
    if time.monotonic() >= deadline:
        raise RuntimeSafetyError("pause consumption budget exhausted")
    pause = root / "pause.json"
    claimed = root / ("pause.consuming." + uuid.uuid4().hex + ".json")
    try:
        pause.rename(claimed)
    except FileNotFoundError:
        return True
    current = _bounded_read(_pause_snapshot, (claimed,), deadline, "claimed pause control read")
    if current is None:
        raise RuntimeSafetyError("claimed pause request disappeared")
    renewed = current["identity"] != observed["identity"]
    if renewed:
        try:
            _write(pause, current["marker"], exclusive=True)
        except FileExistsError:
            pass  # Another new pause is already present; never overwrite it.
    claimed.unlink()
    return not renewed


def _blocked_reason(root):
    path = root / "blocked.json"
    if not path.exists() and not path.is_symlink():
        return None
    marker = _read(path)
    if not isinstance(marker, dict) or marker.get("schema_version") != "1.0" or marker.get("status") != "blocked" or not isinstance(marker.get("reason"), str) or not marker["reason"]:
        raise RuntimeSafetyError("corrupted blocked control")
    return marker["reason"]


def read_status(root: Path, probe_budget_seconds: float = 1.0) -> dict:
    """Inspect without mutations, bounding all reads by an explicit probe budget.

    The default one-second status probe is an uncertainty limit, unrelated to
    solver wall/shutdown limits. A slow read returns blocked and performs no
    worker or cleanup action. Callers may provide a longer inspection budget.
    """
    if isinstance(probe_budget_seconds, bool) or not isinstance(probe_budget_seconds, (int, float)) or not math.isfinite(probe_budget_seconds) or probe_budget_seconds <= 0:
        raise RuntimeSafetyError("status probe budget must be positive and finite")
    deadline = time.monotonic() + probe_budget_seconds
    thread, returned = _async_call(_read_status, Path(root), deadline)
    thread.join(max(0, deadline - time.monotonic()))
    if thread.is_alive() or "value" not in returned:
        return {"status": "blocked", "reason": "status probe timed out or failed",
                "cases": [], "backend_evidence": "unknown", "physical_certification": False}
    return returned["value"]


def _read_status(root: Path, deadline: float) -> dict:
    """Read-only status body; no thread may mutate any persisted control."""
    root = Path(root)
    result = {"status": "not_started", "reason": "", "cases": [],
              "backend_evidence": "unknown", "physical_certification": False}
    try:
        if root.is_symlink():
            raise RuntimeSafetyError("run root symlink is not owned")
        if not root.exists():
            return result
        blocked = _blocked_reason(root)
        if blocked is not None:
            result.update(status="blocked", reason=blocked)
            return result
        paused = _paused(root)
        if (root / "controller.lock").exists():
            raise RuntimeSafetyError("controller lock exists; closure or activity is unverified")
        state_path = root / "state.json"
        if state_path.exists():
            state = _read(state_path)
            if not isinstance(state, dict) or state.get("schema_version") != "1.0" or state.get("status") not in (
                    "pending", "running", "completed", "failed", "paused", "blocked"):
                raise RuntimeSafetyError("run state corrupted")
            records = state.get("cases")
            if not isinstance(records, list) or not records:
                raise RuntimeSafetyError("run case list corrupted")
            if state["status"] == "completed":
                cases = []
                for record in records:
                    attempts = record.get("attempts")
                    if not isinstance(attempts, list) or not attempts:
                        raise RuntimeSafetyError("completed run missing attempts")
                    directory = root / f"cases/{record['id']}/attempt-0001"
                    if not _within(directory, root):
                        raise RuntimeSafetyError("case path is outside owned run")
                    cases.append(_read(_artifact(directory, "inputs.json")))
                inputs_hash = _digest({"schema_version": "1.0", "cases": cases, "scope_mode": state.get("scope_mode")})
                _cache_valid(root, state, cases, inputs_hash, deadline=deadline)
                if any(record.get("status") != "completed" for record in records):
                    raise RuntimeSafetyError("completed run has incomplete case")
            result.update(status=state["status"], reason=state.get("reason", ""), cases=records,
                          backend_evidence=state.get("backend_evidence", "unknown"))
        if paused:
            result["status"] = "paused"
    except Exception as error:
        result.update(status="blocked", reason=str(error))
    return result


def verify_owned(session: dict, live: dict, run_dir: Path) -> bool:
    """Compare injected process observations with a recorded owned identity.

    PID alone is never ownership. Neither this function nor safe_cleanup queries
    any process. run_dir must exactly match both absolute recorded paths.
    """
    try:
        if not isinstance(session, dict) or not isinstance(live, dict):
            return False
        if not isinstance(session.get("session_id"), str) or not session["session_id"]:
            return False
        if type(session.get("pid")) is not int or session["pid"] <= 0:
            return False
        if not session.get("created_at") or not isinstance(session["created_at"], (str, int, float)):
            return False
        root = Path(run_dir)
        if root.is_symlink() or not root.is_dir():
            return False
        for record in (session, live):
            directory = Path(record["run_dir"])
            executable = Path(record["executable"])
            if not directory.is_absolute() or directory.is_symlink() or directory.resolve() != root.resolve():
                return False
            if not executable.is_absolute() or not str(executable):
                return False
        return all(session[key] == live[key] for key in
                   ("session_id", "pid", "created_at", "executable", "run_dir"))
    except (KeyError, TypeError, ValueError, OSError):
        return False


def safe_cleanup(session: dict, live: dict, run_dir: Path, action: Callable) -> bool:
    """Call a per-identity injected action only after complete ownership match."""
    if not verify_owned(session, live, run_dir):
        return False
    try:
        receipt = action(copy.deepcopy(session))
        return isinstance(receipt, dict) and receipt.get("owned_closed") is True
    except Exception:
        return False


def _default_resources(root):
    # Inspect only host memory and the actual output filesystem, never processes.
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong) for name in
                ("total", "available", "total_page", "available_page", "total_virtual", "available_virtual", "extended")]
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError("RAM probe unavailable")
        available = status.available
    else:
        available = os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    return {"available_RAM_GiB": available / 1024**3,
            "free_disk_GiB": shutil.disk_usage(root).free / 1024**3}


def _async_call(function, *args, on_dispatch=None):
    completed, entered = threading.Event(), threading.Event()
    result = {"_completed": completed, "_entered": entered}
    def call():
        entered.set()
        try:
            result["value"] = function(*args)
        except BaseException as error:
            result["error"] = error
        finally:
            completed.set()
    thread = threading.Thread(target=call, daemon=True)
    if on_dispatch is not None:
        # Bind lifetime before Thread.start: interruption immediately after
        # dispatch must not lose its stop event or cleanup callback reference.
        try:
            on_dispatch(thread, result)
        except BaseException as error:
            # Thread.start has not been called, so no launch is possible here.
            result.update(error=error, _no_launch_proven=True)
            completed.set()
            raise
    try:
        thread.start()
    except BaseException as error:
        # Thread.start can be interrupted after OS thread creation but before
        # its Python handshake. is_alive()==False cannot prove no launch.
        # Retain uncertainty until the target's actual finally block completes.
        result["_start_error"] = error
        raise
    return thread, result


def _dispatch_pending(thread, returned):
    completed = returned.get("_completed")
    return not isinstance(completed, threading.Event) or not completed.is_set() or thread.is_alive()


def _dispatch_startup_unknown(returned):
    entered = returned.get("_entered")
    return ("_start_error" in returned
            and isinstance(entered, threading.Event) and not entered.is_set())


class RunSupervisor:
    """Retain native lifetime after the controller's bounded work deadline.

    Only injected worker hooks observe or close their owned session. A timeout
    means uncertainty, never permission to abandon a request or kill a process.
    ``wait`` therefore remains active until the hooks prove safe teardown; an
    unknown session may require human intervention. Controller receipts, locks,
    pause requests and failure budgets are never upgraded by late supervision.
    """
    def __init__(self, *, poll_interval_seconds=.2, callback_timeout_seconds=10):
        for value in (poll_interval_seconds, callback_timeout_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise RuntimeSafetyError("supervision timing must be positive and finite")
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.callback_timeout_seconds = float(callback_timeout_seconds)
        self.started = False
        self._finished = False
        self._workers = []
        self._cleanups = []
        self._call = None
        self._root = None
        self._worker = None
        self._approved = False
        self._backend_status = None
        self._closure = None
        self._errors = []
        self._last_cleanup = -math.inf

    def begin(self, worker, root):
        if self.started:
            raise RuntimeSafetyError("a supervisor belongs to exactly one controller invocation")
        self.started, self._worker, self._root = True, worker, Path(root).resolve()

    def track_worker(self, thread, returned, stop, directory):
        self._workers.append((thread, returned, stop, Path(directory)))

    def track_cleanup(self, thread, returned):
        self._cleanups.append((thread, returned))

    def finish_controller(self):
        self._finished = True
        if self.pending_actions():
            self._stop()

    def snapshot(self):
        workers = sum(_dispatch_pending(thread, returned) for thread, returned, *_ in self._workers)
        cleanups = sum(_dispatch_pending(thread, returned) for thread, returned in self._cleanups)
        callback = int(self._call is not None and _dispatch_pending(self._call[1], self._call[2]))
        unknown = sum(_dispatch_startup_unknown(returned) for _, returned, *_ in self._workers)
        unknown += sum(_dispatch_startup_unknown(returned) for _, returned in self._cleanups)
        unknown += int(self._call is not None and _dispatch_startup_unknown(self._call[2]))
        return {"schema_version": "cst-supervision/1", "status": "closed" if self._approved else "blocked",
                "exit_ready": self._approved, "controller_pending": not self._finished,
                "worker_pending": workers, "cleanup_pending": cleanups,
                "supervision_callback_pending": callback,
                "startup_outcome_unknown": unknown,
                "no_dispatch_proven": self.started and self._finished and not self._workers,
                "owned_closed": self._approved,
                "backend_status": copy.deepcopy(self._backend_status),
                "closure": copy.deepcopy(self._closure),
                "diagnostic_errors": list(self._errors),
                "physical_certification": False}

    def pending_actions(self):
        return any(_dispatch_pending(thread, returned) for thread, returned, *_ in self._workers) or self.pending_cleanup()

    def pending_cleanup(self):
        return any(_dispatch_pending(thread, returned) for thread, returned in self._cleanups)

    def _stop(self):
        for _, _, stop, _ in self._workers:
            stop.set()

    def _error(self, error):
        self._stop()
        self._errors.append({"type": type(error).__name__, "message": str(error)[:512]})
        self._errors = self._errors[-16:]

    def _publish(self, snapshot, on_update):
        # Diagnostics cannot control native lifetime, even if storage is denied
        # or a user interrupts a print/update callback.
        try:
            if self._root is not None:
                _write(self._root / "supervision.json", snapshot)
        except BaseException as error:
            self._error(error)
        if on_update is not None:
            try:
                on_update(copy.deepcopy(snapshot))
            except BaseException as error:
                self._error(error)
        snapshot["diagnostic_errors"] = list(self._errors)
        return snapshot

    def _invoke(self, kind, function):
        def record(thread, returned):
            self._call = (kind, thread, returned,
                          time.monotonic() + self.callback_timeout_seconds)
        _async_call(function, on_dispatch=record)

    def wait(self, worker=None, *, on_update=None):
        """Drain tracked callbacks and separately verify native owned closure.

        This is deliberately a lifetime wait, independent of the case work
        budget. A hook is never duplicated while its earlier invocation runs.
        Unknown or false closure keeps a blocked status and the interpreter
        alive; no empty/missing receipt is interpreted as absence of a session.
        Workers without the native supervision protocol remain bounded and
        receive an unconfirmed return instead of a persistent native wait.
        """
        backend = worker if worker is not None else self._worker
        query = getattr(backend, "supervision_status", None)
        cleanup = getattr(backend, "supervise_cleanup", None)
        if not self.started:
            raise RuntimeSafetyError("supervisor has not observed a controller invocation")
        if backend is not self._worker:
            raise RuntimeSafetyError("supervisor worker identity changed")
        if not callable(query) or not callable(cleanup):
            snapshot = self.snapshot()
            snapshot.update(reason="native supervision hooks unavailable", requires_user_intervention=True)
            return self._publish(snapshot, on_update)
        reason = "waiting for dispatched worker and cleanup callbacks"
        intervention = False
        while True:
            try:
                snapshot = self.snapshot()
                if snapshot["controller_pending"] or snapshot["worker_pending"] or snapshot["cleanup_pending"]:
                    snapshot.update(reason=("dispatch startup outcome remains unknown; lifetime retained"
                                            if snapshot["startup_outcome_unknown"] else reason),
                                    requires_user_intervention=bool(snapshot["startup_outcome_unknown"]))
                    self._publish(snapshot, on_update)
                    threading.Event().wait(self.poll_interval_seconds)
                    continue
                if self._call is None:
                    self._invoke("status", query)
                kind, thread, returned, call_deadline = self._call
                if _dispatch_pending(thread, returned):
                    snapshot = self.snapshot()
                    snapshot.update(reason="owned supervision callback remains pending",
                                    requires_user_intervention=time.monotonic() >= call_deadline)
                    self._publish(snapshot, on_update)
                    completed = returned.get("_completed")
                    if isinstance(completed, threading.Event):
                        completed.wait(self.poll_interval_seconds)
                    else:
                        threading.Event().wait(self.poll_interval_seconds)
                    continue
                self._call = None
                if "error" in returned:
                    self._error(returned["error"])
                    reason, intervention = "owned supervision callback failed; closure remains unknown", True
                elif kind == "cleanup":
                    value = returned.get("value")
                    self._closure = copy.deepcopy(value) if isinstance(value, dict) else None
                    # Requery after every cleanup; a returned true receipt alone
                    # cannot override a still pending request or contradiction.
                    reason = "rechecking owned lifetime after supervised cleanup"
                    intervention = not isinstance(value, dict) or value.get("owned_closed") is not True
                else:
                    value = returned.get("value")
                    valid = (isinstance(value, dict)
                             and all(type(value.get(key)) is bool for key in
                                     ("request_pending", "session_creation_pending", "owned_closed"))
                             and (type(value.get("session_created")) is bool or value.get("session_created") == "unknown")
                             and ("worker_pending" not in value or type(value["worker_pending"]) is bool))
                    if not valid:
                        self._backend_status = None
                        reason, intervention = "owned supervision evidence is missing or invalid", True
                    else:
                        self._backend_status = copy.deepcopy(value)
                        pending = value["request_pending"] or value["session_creation_pending"] or value.get("worker_pending", False)
                        if pending:
                            reason, intervention = "owned startup or SDK request remains pending", False
                        elif value["owned_closed"] is True and type(value["session_created"]) is bool:
                            self._approved = True
                            final = self.snapshot()
                            final.update(reason="owned lifetime closure verified", requires_user_intervention=False)
                            return self._publish(final, on_update)
                        elif value["session_created"] is True or value.get("identity_recorded") is True:
                            reason, intervention = "owned closure remains unconfirmed", True
                            if time.monotonic() - self._last_cleanup >= self.callback_timeout_seconds:
                                self._last_cleanup = time.monotonic()
                                self._invoke("cleanup", lambda: cleanup(timeout_seconds=self.callback_timeout_seconds))
                        else:
                            reason, intervention = "session creation remains unknown; human intervention required", True
                snapshot = self.snapshot()
                snapshot.update(reason=reason, requires_user_intervention=intervention)
                self._publish(snapshot, on_update)
                threading.Event().wait(self.poll_interval_seconds)
            except BaseException as error:
                self._error(error)
                # Keep the existing request reference; its completion is still
                # required before any subsequent observation/cleanup dispatch.
                reason, intervention = "supervision interrupted; owned lifetime still retained", True


def _check_assets_bounded(case, deadline):
    _bounded_read(_check_assets, (case,), deadline, "STL asset check")


def _bounded_read(function, args, deadline, label):
    """Wait only within the shared deadline; abandoned calls are read-only.

    No worker, cleanup, control write or receipt publication is dispatched by
    this helper. A late result cannot upgrade controller state or consume cache.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeSafetyError(label + " timed out: budget exhausted")
    thread, returned = _async_call(function, *args)
    thread.join(max(0, deadline - time.monotonic()))
    if thread.is_alive():
        raise RuntimeSafetyError(label + " timed out")
    if "error" in returned:
        raise RuntimeSafetyError(str(returned["error"]))
    if time.monotonic() >= deadline:
        raise RuntimeSafetyError(label + " timed out: budget exhausted")
    return returned["value"]


def _hash_bounded(path, deadline):
    return _bounded_read(_file_hash, (path,), deadline, "file hash")


def _read_bounded(path, deadline):
    return _bounded_read(_read, (path,), deadline, "file read")


def _artifact_hash_bounded(directory, name, deadline):
    def inspect():
        return _file_hash(_artifact(directory, name))
    return _bounded_read(inspect, (), deadline, "artifact hash")


def _load_cache(root, state_path, cases, inputs_hash, deadline):
    # All operations here are read-only, including a late/abandoned validation.
    return _cache_valid(root, _read(state_path), cases, inputs_hash, deadline=deadline)


def _probe(probe, root, limits, deadline):
    thread, result = _async_call(probe, root)
    thread.join(max(0, min(1.0, deadline - time.monotonic())))
    if thread.is_alive():
        raise RuntimeSafetyError("resource probe timed out")
    if "error" in result:
        raise RuntimeSafetyError("resource probe failed")
    reading = result.get("value")
    if not isinstance(reading, dict):
        raise RuntimeSafetyError("resource probe returned invalid data")
    for reading_key, limit_key in (("available_RAM_GiB", "min_available_RAM_GiB"),
                                  ("free_disk_GiB", "min_free_disk_GiB")):
        value = reading.get(reading_key)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < limits[limit_key]:
            raise RuntimeSafetyError("resource probe insufficient or invalid: " + reading_key)


def _validate(plan):
    if not isinstance(plan, dict) or plan.get("schema_version") != "1.0":
        raise RuntimeSafetyError("prepared plan schema_version must be 1.0")
    cases = plan.get("cases")
    if not isinstance(cases, list) or not cases or len(cases) > 10000:
        raise RuntimeSafetyError("prepared cases must be a finite nonempty explicit list")
    mode = plan.get("scope_mode")
    if mode not in ("single", "batch") or (mode == "single" and len(cases) != 1):
        raise RuntimeSafetyError("single scope must contain exactly one case")
    names = []
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", case["id"]):
            raise RuntimeSafetyError("case id must be a safe unique string")
        names.append(case["id"])
    if len(names) != len(set(names)):
        raise RuntimeSafetyError("case ids must be unique")
    limits = copy.deepcopy(plan.get("runtime", {}))
    for key, default in (("max_attempts", 1), ("min_available_RAM_GiB", 0), ("min_free_disk_GiB", 0)):
        limits.setdefault(key, default)
    for key in ("wall_budget_seconds", "max_cpus", "max_modes", "max_attempts", "min_available_RAM_GiB", "min_free_disk_GiB"):
        value = limits.get(key)
        minimum = 0 if key.startswith("min_") else 0.0000001
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
            raise RuntimeSafetyError("invalid runtime limit: " + key)
        if key in ("max_cpus", "max_modes", "max_attempts") and type(value) is not int:
            raise RuntimeSafetyError("runtime count must be an integer: " + key)
    if limits["max_attempts"] > 1000:
        raise RuntimeSafetyError("max_attempts exceeds controller bound")
    _bytes(cases)
    return copy.deepcopy(cases), limits, _digest({"schema_version": "1.0", "cases": cases, "scope_mode": mode})


def _cache_valid(root, state, cases, inputs_hash, deadline):
    if not isinstance(state, dict) or state.get("schema_version") != "1.0" or state.get("inputs_hash") != inputs_hash:
        raise RuntimeSafetyError("resume input content changed or state corrupted")
    records = state.get("cases")
    if not isinstance(records, list) or [r.get("id") for r in records if isinstance(r, dict)] != [c["id"] for c in cases]:
        raise RuntimeSafetyError("resume case list corrupted")
    for record, case in zip(records, cases):
        attempts = record.get("attempts")
        if not isinstance(attempts, list):
            raise RuntimeSafetyError("resume attempts corrupted")
        for number, attempt in enumerate(attempts, 1):
            expected = f"cases/{record['id']}/attempt-{number:04d}"
            if not isinstance(attempt, dict) or attempt.get("directory") != expected or attempt.get("owned_closed") is not True:
                raise RuntimeSafetyError("resume blocked by unconfirmed closure or attempt corruption")
            directory = root / expected
            inputs = _artifact(directory, "inputs.json")
            if _read(inputs) != case or _hash_bounded(inputs, deadline) != attempt.get("input_file_hash"):
                raise RuntimeSafetyError("immutable input snapshot changed")
            runtime_snapshot = _artifact(directory, "runtime.json")
            if not isinstance(_read(runtime_snapshot), dict) or _hash_bounded(runtime_snapshot, deadline) != attempt.get("runtime_file_hash"):
                raise RuntimeSafetyError("immutable runtime snapshot changed")
            receipt_path = _artifact(directory, "receipt.json")
            if _hash_bounded(receipt_path, deadline) != attempt.get("receipt_file_hash"):
                raise RuntimeSafetyError("attempt receipt changed")
            receipt = _read(receipt_path)
            if receipt.get("status") != attempt.get("status") or receipt.get("owned_closed") is not True:
                raise RuntimeSafetyError("attempt receipt corrupted")
            if receipt.get("status") == "paused":
                unstarted = (receipt.get("controller_not_started") is True
                             and attempt.get("worker_dispatched") is False
                             and receipt.get("reason") == "controller_pause_before_dispatch"
                             and "worker_status" not in receipt
                             and "interruption_acknowledged" not in receipt)
                cancelled = (receipt.get("worker_status") == "failed"
                             and receipt.get("interruption_acknowledged") is True
                             and "controller_not_started" not in receipt)
                if receipt.get("interruption") != "pause requested" or not (unstarted or cancelled):
                    raise RuntimeSafetyError("paused receipt lacks controller interruption evidence")
            retryable = receipt.get("retryable", True)
            if type(retryable) is not bool or type(attempt.get("retryable", True)) is not bool or attempt.get("retryable", True) is not retryable:
                raise RuntimeSafetyError("attempt retryable flag invalid or corrupted")
            hashes = attempt.get("artifact_hashes")
            if not isinstance(hashes, dict) or not isinstance(receipt.get("artifacts"), list) or len(receipt["artifacts"]) != len(hashes) or set(receipt["artifacts"]) != set(hashes):
                raise RuntimeSafetyError("cached artifact list corrupted")
            for name, digest in hashes.items():
                if _artifact_hash_bounded(directory, name, deadline) != digest:
                    raise RuntimeSafetyError("cached output content changed")
        expected_status = attempts[-1]["status"] if attempts else "pending"
        if record.get("status") != expected_status:
            raise RuntimeSafetyError("cached case status corrupted")
    # Snapshot/manifest integrity must precede following any recorded source path.
    for case in cases:
        _check_assets_bounded(case, deadline)
    return state


def _cleanup(worker, directory, deadline, supervisor=None):
    if supervisor is not None and supervisor.pending_cleanup():
        return False  # An interrupted earlier launch may still enter later.
    ownership = getattr(worker, "ownership", None)
    action = getattr(worker, "cleanup_identity", None)
    if not callable(ownership) or not callable(action):
        return False
    def close():
        session, live = ownership(directory)
        return safe_cleanup(session, live, directory, action)
    if supervisor is not None:
        thread, result = _async_call(close, on_dispatch=supervisor.track_cleanup)
    else:
        thread, result = _async_call(close)
    thread.join(max(0, deadline - time.monotonic()))
    return not thread.is_alive() and result.get("value") is True


def run_cases(plan: dict, output_root: Path, worker: Callable,
              resource_probe: Callable | None = None, resume: bool = False,
              supervisor: RunSupervisor | None = None) -> dict:
    """Run exactly the prepared list; completed resumes validate content hashes.

    Return status, reason, case records and backend evidence. This orchestration
    makes no claim of numerical or physical certification. A retained lock means
    recovery requires external verification of closure, never automatic killing.
    An unfinished read-only hash with verified worker closure may release the
    process lock, but a durable blocked.json prevents cache/worker recovery.
    Read-only task results never write controls or publish terminal receipts.
    A failed worker receipt may set retryable=False to stop attempts, including
    explicit resume. An absent flag retains the authorized max_attempts policy.
    """
    cases, limits, inputs_hash = _validate(plan)
    if not callable(worker):
        raise RuntimeSafetyError("worker must be callable")
    if supervisor is not None and not isinstance(supervisor, RunSupervisor):
        raise RuntimeSafetyError("supervisor must be a RunSupervisor")
    root = Path(output_root)
    state = None
    locked = False
    keep_lock = False
    active = None
    final_outcome = None
    def outcome(status, reason=""):
        nonlocal final_outcome
        final_outcome = {"status": status, "reason": reason, "cases": state["cases"] if state else [],
                         "backend_evidence": state.get("backend_evidence", "unknown") if state else "unknown",
                         "physical_certification": False}
        return final_outcome
    try:
        # Arm finally before recording a started controller. Interruption in
        # its clock/path setup must not leave controller_pending forever.
        if supervisor is not None:
            supervisor.begin(worker, root)
        deadline = time.monotonic() + limits["wall_budget_seconds"]
        if root.is_symlink():
            raise RuntimeSafetyError("run root symlink is not owned")
        root.mkdir(parents=True, exist_ok=True)
        root = root.resolve()
        blocked = _bounded_read(_blocked_reason, (root,), deadline, "blocked control read")
        if blocked is not None:
            return outcome("blocked", blocked)
        pause_observed = _bounded_read(_pause_snapshot, (root / "pause.json",), deadline, "pause control read")
        try:
            _write(root / "controller.lock", {"session_id": uuid.uuid4().hex}, exclusive=True)
            locked = True
        except FileExistsError:
            return outcome("blocked", "controller lock exists; ownership closure must be verified")
        for case in cases:
            _check_assets_bounded(case, deadline)
        state_path = root / "state.json"
        if state_path.exists():
            if not resume and pause_observed is None:
                raise RuntimeSafetyError("run exists; explicit resume required")
            state = _bounded_read(_load_cache, (root, state_path, cases, inputs_hash, deadline),
                                  deadline, "resume cache validation")
            if state.get("status") == "blocked":
                raise RuntimeSafetyError("blocked run requires closure review")
        else:
            # Never consume orphan attempts or a root from an unrelated run.
            if (root / "cases").exists():
                raise RuntimeSafetyError("orphan attempts require ownership review")
            state = {"schema_version": "1.0", "inputs_hash": inputs_hash, "scope_mode": plan["scope_mode"], "status": "pending",
                     "backend_evidence": "unknown", "cases": [
                         {"id": case["id"], "status": "pending", "attempts": []} for case in cases]}
            _write(state_path, state)
        if pause_observed is not None:
            if not resume or not _consume_pause(root, pause_observed, deadline):
                state["status"] = "paused"
                _write(state_path, state)
                return outcome("paused", "pause requested")
        # Requests arriving after the captured one still apply to this resume.
        if _bounded_read(_paused, (root,), deadline, "pause control read"):
            state["status"] = "paused"
            _write(state_path, state)
            return outcome("paused", "pause requested")
        probe = resource_probe or _default_resources
        for case, record in zip(cases, state["cases"]):
            if record["status"] == "completed":
                continue
            if record["status"] == "failed" and record["attempts"] and record["attempts"][-1].get("retryable", True) is False:
                prior_receipt = _read_bounded(root / record["attempts"][-1]["directory"] / "receipt.json", deadline)
                state["status"] = "failed"
                _write(state_path, state)
                return outcome("failed", "nonretryable worker failure: " + str(prior_receipt.get("reason", "unspecified")))
            # Only controller-proven, closed pause cancellations are uncharged.
            while sum(a["status"] != "paused" for a in record["attempts"]) < limits["max_attempts"]:
                if _bounded_read(_paused, (root,), deadline, "pause control read"):
                    state["status"] = "paused"
                    _write(state_path, state)
                    return outcome("paused")
                if time.monotonic() >= deadline:
                    raise RuntimeSafetyError("wall budget exhausted")
                _probe(probe, root, limits, deadline)
                if _bounded_read(_paused, (root,), deadline, "pause control read after resource probe"):
                    state["status"] = "paused"
                    _write(state_path, state)
                    return outcome("paused", "pause requested")
                if time.monotonic() >= deadline:
                    raise RuntimeSafetyError("wall budget exhausted before worker start")
                number = len(record["attempts"]) + 1
                relative = f"cases/{case['id']}/attempt-{number:04d}"
                directory = root / relative
                if not _within(directory, root) or (directory.parent.exists() and directory.parent.is_symlink()):
                    raise RuntimeSafetyError("attempt path is outside owned root")
                directory.mkdir(parents=True, exist_ok=False)
                _write(directory / "inputs.json", case, exclusive=True)
                _write(directory / "runtime.json", limits, exclusive=True)
                attempt = {"directory": relative, "status": "running", "owned_closed": False,
                           "worker_dispatched": False,
                           "input_file_hash": _hash_bounded(directory / "inputs.json", deadline),
                           "runtime_file_hash": _hash_bounded(directory / "runtime.json", deadline)}
                record["attempts"].append(attempt)
                record["status"] = "running"
                state["status"] = "running"
                _write(state_path, state)
                stop = threading.Event()
                _check_assets_bounded(case, deadline)
                before_dispatch_pause = _bounded_read(_paused, (root,), deadline, "pause control read before dispatch")
                if before_dispatch_pause:
                    # A staged attempt never reached the worker. Preserve that
                    # fact separately from a worker-acknowledged cancellation.
                    thread = None
                    returned = {"value": {"status": "failed", "owned_closed": True,
                                          "artifacts": [], "controller_not_started": True,
                                          "reason": "controller_pause_before_dispatch"}}
                    interrupt = "pause requested"
                else:
                    attempt["worker_dispatched"] = True
                    _write(state_path, state)
                    if supervisor is not None:
                        thread, returned = _async_call(worker, copy.deepcopy(case), directory, stop,
                            on_dispatch=lambda dispatched, result: supervisor.track_worker(
                                dispatched, result, stop, directory))
                    else:
                        thread, returned = _async_call(worker, copy.deepcopy(case), directory, stop)
                    active = (thread, returned, stop, directory)
                    interrupt = None
                while thread is not None and thread.is_alive():
                    thread.join(max(0, min(.02, deadline - time.monotonic())))
                    if not thread.is_alive():
                        break
                    try:
                        if time.monotonic() >= deadline:
                            interrupt = "wall budget exhausted"
                        elif _bounded_read(_paused, (root,), deadline, "active pause control read"):
                            interrupt = "pause requested"
                        else:
                            _probe(probe, root, limits, deadline)
                    except Exception as error:
                        interrupt = str(error)
                    if interrupt:
                        break
                if interrupt and thread is not None:
                    stop.set()
                    closure_deadline = time.monotonic() + .2
                    cleanup_closed = _cleanup(worker, directory, closure_deadline, supervisor)
                    thread.join(max(0, closure_deadline - time.monotonic()))
                else:
                    cleanup_closed = False
                receipt = returned.get("value")
                closed = (isinstance(receipt, dict) and receipt.get("owned_closed") is True) or cleanup_closed
                if (thread is not None and thread.is_alive()) or not closed:
                    keep_lock = True
                    attempt["status"] = "blocked"
                    record["status"] = "blocked"
                    if interrupt:
                        # This interruption already performed its bounded cleanup.
                        active = None
                    raise RuntimeSafetyError("worker closure unconfirmed" + (": " + interrupt if interrupt else ""))
                active = None
                if not isinstance(receipt, dict) or receipt.get("status") not in ("completed", "failed"):
                    raise RuntimeSafetyError("worker returned invalid receipt")
                if thread is not None and "controller_not_started" in receipt:
                    raise RuntimeSafetyError("worker cannot declare a controller-only unstarted receipt")
                if "retryable" in receipt and type(receipt["retryable"]) is not bool:
                    raise RuntimeSafetyError("worker retryable flag must be boolean")
                if interrupt == "pause requested":
                    receipt = {**receipt, "owned_closed": True, "interruption": interrupt}
                    if before_dispatch_pause:
                        receipt["status"] = "paused"
                    else:
                        receipt["worker_status"] = receipt["status"]
                    if receipt["status"] == "failed" and receipt.get("interruption_acknowledged") is True:
                        receipt["status"] = "paused"
                elif interrupt:
                    receipt = {**receipt, "status": "failed", "owned_closed": True}
                artifacts = receipt.get("artifacts")
                if not isinstance(artifacts, list) or len(artifacts) != len(set(artifacts)):
                    raise RuntimeSafetyError("worker receipt requires unique relative artifacts list")
                hashes = {name: _artifact_hash_bounded(directory, name, deadline) for name in artifacts}
                _check_assets_bounded(case, deadline)
                if _read_bounded(directory / "inputs.json", deadline) != case or _hash_bounded(directory / "inputs.json", deadline) != attempt["input_file_hash"]:
                    raise RuntimeSafetyError("worker changed immutable input snapshot")
                if _read_bounded(directory / "runtime.json", deadline) != limits or _hash_bounded(directory / "runtime.json", deadline) != attempt["runtime_file_hash"]:
                    raise RuntimeSafetyError("worker changed immutable runtime snapshot")
                # Publish the terminal receipt only after its staged bytes have
                # been verified within the same deadline as every result hash.
                pending_receipt = directory / "receipt.pending.json"
                _write(pending_receipt, receipt, exclusive=True)
                receipt_hash = _hash_bounded(pending_receipt, deadline)
                receipt_path = directory / "receipt.json"
                if receipt_path.exists():
                    raise RuntimeSafetyError("terminal receipt already exists")
                if time.monotonic() >= deadline:
                    raise RuntimeSafetyError("receipt publication timed out")
                pending_receipt.rename(receipt_path)
                attempt.update(status=receipt["status"], owned_closed=True, artifact_hashes=hashes,
                               receipt_file_hash=receipt_hash,
                               retryable=receipt.get("retryable", True))
                record["status"] = receipt["status"]
                state["backend_evidence"] = receipt.get("backend_evidence", "unknown")
                _write(state_path, state)
                if interrupt:
                    state["status"] = "paused" if interrupt == "pause requested" else "blocked"
                    _write(state_path, state)
                    return outcome(state["status"], interrupt)
                if record["status"] == "completed":
                    break
                if receipt.get("retryable", True) is False:
                    state["status"] = "failed"
                    _write(state_path, state)
                    return outcome("failed", "nonretryable worker failure: " + str(receipt.get("reason", "unspecified")))
            if record["status"] != "completed":
                state["status"] = "failed"
                _write(state_path, state)
                return outcome("failed", "authorized attempts exhausted")
        state["status"] = "completed"
        _write(state_path, state)
        return outcome("completed")
    except (Exception, KeyboardInterrupt) as error:
        reason = str(error) or type(error).__name__
        if active is not None:
            active_thread, active_result, active_stop, active_directory = active
            active_stop.set()
            closure_deadline = time.monotonic() + .2
            confirmed = _cleanup(worker, active_directory, closure_deadline, supervisor)
            active_thread.join(max(0, closure_deadline - time.monotonic()))
            active_receipt = active_result.get("value")
            confirmed = confirmed or (isinstance(active_receipt, dict) and active_receipt.get("owned_closed") is True)
            keep_lock = keep_lock or active_thread.is_alive() or not confirmed
        if state is not None and locked:
            state["status"] = "blocked"
            state["reason"] = reason
            try:
                _write(root / "state.json", state)
            except Exception:
                keep_lock = True
        if locked:
            try:
                _write(root / "blocked.json", {"schema_version": "1.0", "status": "blocked", "reason": reason})
            except Exception:
                # Even a confirmed worker must not recover when durable I/O
                # blocking could not be recorded on this filesystem.
                keep_lock = True
        return outcome("blocked", reason)
    finally:
        if supervisor is not None:
            keep_lock = keep_lock or supervisor.pending_actions()
            supervisor.finish_controller()
            if final_outcome is not None:
                final_outcome["supervision"] = supervisor.snapshot()
                final_outcome["exit_ready"] = final_outcome["supervision"]["exit_ready"]
        if locked and not keep_lock:
            (root / "controller.lock").unlink(missing_ok=True)
