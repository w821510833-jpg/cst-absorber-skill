"""Lazy CST SDK transport for a fresh, explicitly authorized environment.

The imported vendor surface is restricted to public CST 2025 Python interface
methods. The SDK's ``timeout: int`` unit is undocumented in the inspected help:
it is deliberately left at its default, rather than guessing milliseconds or
seconds. ``timeout_seconds`` bounds this caller with a monotonic deadline and a
daemon thread. A request may continue after that deadline; no other SDK request
is issued while it is pending, and closure stays unconfirmed.

The Windows observer below reads only the PID returned by this newly created DE,
using PROCESS_QUERY_LIMITED_INFORMATION. It neither enumerates nor terminates
processes. Offline tests inject both the interface and observer; they establish
call/safety behavior, not native execution, process closure or physical validity.

The held project's control plane (poll/info/abort/documented without-saving
close) keeps the complete owned process/object/path/filename boundary. Its
archive may be mutable. Content operations instead require the explicitly
saved inode and stable SHA256 snapshot; a solver-time replacement is quarantined
without assuming CST was its writer. Successful closure is not archive or
physics acceptance. Only a successful, bounded explicit SDK save can commit a
new pin. Native asynchronous writer attribution remains unresolved.
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import math
import os
from pathlib import Path
import stat
import threading
import time
import uuid


DEFAULT_REQUEST_SECONDS = 30.0
DEFAULT_CLEANUP_SECONDS = 10.0
IDENTITY_KEYS = ("session_id", "pid", "created_at", "executable", "run_dir")


class SessionSafetyError(ValueError):
    """An ownership/lifecycle check failed; no unsafe operation is permitted."""


class SessionCancelled(SessionSafetyError):
    """A stop/cancellation guard prevented work, distinct from a failed check."""


class SessionTimeout(TimeoutError):
    """A caller deadline elapsed, with the session attached for safe recovery."""


def _seconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise SessionSafetyError("timeout_seconds must be a positive finite number in seconds")
    return float(value)


def _linked(path):
    # Junctions are links on Windows but Path.is_symlink alone does not find all.
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _windows_owned_pid_identity(pid):
    """Observe exactly one known owned PID, never an existing-session search.

    States are alive (with PID, creation FILETIME and executable), gone, unknown.
    OpenProcess access denial and all incomplete queries return unknown. Only an
    invalid PID or an observed exited handle establishes gone. The handle ties
    the time/path queries to one process instance even if the PID is later reused.
    This real observer must not be called by offline tests.
    """
    if type(pid) is not int or pid <= 0 or pid > 0xFFFFFFFF:
        return {"state": "unknown", "pid": pid, "reason": "invalid owned PID"}
    if os.name != "nt":
        return {"state": "unknown", "pid": pid, "reason": "Windows identity observer required"}
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        error = ctypes.get_last_error()
        return {"state": "gone" if error == 87 else "unknown", "pid": pid,
                "reason": "OpenProcess error " + str(error)}
    try:
        exit_code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return {"state": "unknown", "pid": pid, "reason": "GetExitCodeProcess failed"}
        if exit_code.value != 259:  # STILL_ACTIVE
            return {"state": "gone", "pid": pid}
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                      ctypes.byref(system), ctypes.byref(user)):
            return {"state": "unknown", "pid": pid, "reason": "GetProcessTimes failed"}
        size = wintypes.DWORD(32768)
        executable = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, executable, ctypes.byref(size)):
            return {"state": "unknown", "pid": pid, "reason": "QueryFullProcessImageNameW failed"}
        filetime = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return {"state": "alive", "pid": pid, "created_at": "filetime:" + str(filetime),
                "executable": executable.value}
    finally:
        kernel.CloseHandle(handle)


class CstSdkSession:
    """Create and use only this new DE/project and its exact owned run path.

    ``identity_probe(pid)`` may be injected and must return a dict in the real
    observer's three-state format. Omitting state on a complete alive identity
    is accepted for the controller's identity-only observations. A probe cannot
    establish ownership using a PID alone. Construction exceptions carry
    ``error.session`` so a controller can retain ownership/uncertain cleanup.
    """

    def __init__(self, run_dir, *, authorized=False, interface_module=None,
                 identity_probe=None, stop_event=None,
                 startup_timeout_seconds=DEFAULT_REQUEST_SECONDS):
        if authorized is not True:
            raise PermissionError("explicit CST execution authorization is required before SDK import")
        budget = _seconds(startup_timeout_seconds)
        self._run_input = Path(run_dir).absolute()
        if any(_linked(path) for path in (self._run_input, *self._run_input.parents)):
            raise SessionSafetyError("owned run must not traverse a symlink or junction")
        self._run_input.mkdir(parents=True, exist_ok=True)
        self.run_dir = self._run_input.resolve()
        self._project_path = self.run_dir / "model.cst"
        if self._project_path.exists() or _linked(self._project_path):
            raise FileExistsError("initial owned project path already exists")
        self._scratch_path = self.run_dir / "temp"
        if _linked(self._scratch_path):
            raise SessionSafetyError("owned scratch path must not be a symlink or junction")
        self._scratch_path.mkdir(exist_ok=True)
        self.evidence_kind = "native_sdk" if interface_module is None else "injected_test_interface"
        self._probe = identity_probe if identity_probe is not None else _windows_owned_pid_identity
        self._stop_event = stop_event if stop_event is not None else threading.Event()
        self._startup_cancelled = threading.Event()
        self._startup_accepted = self._startup_cleanup_attempted = False
        self._state_lock = threading.Lock()
        self._active_thread = None
        self._active_request = None
        self._environment_creation_attempted = False
        self._creation_no_launch_confirmed = False
        self._de = self._project = None
        self._held_de = self._held_project = None
        self._identity = None
        self._saved = self._project_closed = self._de_close_returned = False
        self._project_save_attempted = self._project_location_bound = False
        self._saved_file_identity = None
        self._saved_file_digest = None
        self._archive = {"state": "unsaved", "status": "unsaved",
                         "archive_integrity_verified": False,
                         "writer_attribution": "unresolved"}
        self._readback_outputs = {}
        self._owned_closed = False
        self._confirmed_close = None
        self._last_close = self._receipt(False, "environment not yet closed")
        self._session_id = uuid.uuid4().hex
        if self._stop_event.is_set():
            raise SessionCancelled("startup cancelled before vendor import")
        try:
            self._request("startup", lambda deadline: self._startup(interface_module, deadline), budget,
                          startup=True)
        except BaseException as error:
            error.session = self
            raise

    @property
    def project_path(self):
        return self._project_path

    @property
    def identity(self):
        return copy.deepcopy(self._identity)

    def _receipt(self, closed, reason, **extra):
        return {"owned_closed": closed, "reason": reason,
                "evidence_kind": self.evidence_kind, **extra}

    def _request(self, label, function, timeout_seconds, *, startup=False,
                 on_accept=None, on_reject=None):
        deadline = time.monotonic() + _seconds(timeout_seconds)
        returned = {}
        ready = threading.Event()
        decision = threading.Event()
        completed = threading.Event()
        settled = threading.Event()
        reservation = {"label": label, "startup": startup,
                       "completed": completed, "settled": settled,
                       "caller_state": "active", "launch_state": "reserved"}
        def invoke():
            try:
                returned["value"] = function(deadline)
            except BaseException as error:
                returned["error"] = error
            finally:
                try:
                    if startup:
                        # Acceptance/late cleanup must finish before a second
                        # SDK call. Thread.is_alive() is not launch/completion
                        # proof when Thread.start's handshake is interrupted.
                        ready.set()
                        decision.wait(max(0.0, deadline - time.monotonic()))
                        with self._state_lock:
                            accepted = self._startup_accepted
                            if not accepted:
                                self._startup_cancelled.set()
                        if not accepted:
                            self._startup_cleanup()
                finally:
                    if startup and not self._environment_creation_attempted and self._de is None:
                        with self._state_lock:
                            self._creation_no_launch_confirmed = True
                            self._owned_closed = True
                            self._confirmed_close = self._receipt(
                                True, "startup callback completed without attempting environment creation",
                                closure_kind="creation_not_attempted")
                    # The completion token is published only after every SDK
                    # operation and owned late-cleanup callback has ended.
                    completed.set()
        thread = threading.Thread(target=invoke, daemon=True, name="owned-cst-" + label)
        with self._state_lock:
            if self._active_request is not None and not (
                    self._active_request["completed"].is_set() and self._active_request["settled"].is_set()):
                raise SessionSafetyError("SDK request still pending; concurrent SDK actions are forbidden")
            self._active_request = reservation
            self._active_thread = thread
        try:
            try:
                # The reservation stays visible during a blocked/interrupted
                # start; no state lock is held across that handshake.
                thread.start()
                reservation["launch_state"] = "start_returned"
            except BaseException:
                reservation["launch_state"] = "indeterminate"
                raise
            if startup:
                ready_received = ready.wait(max(0.0, deadline - time.monotonic()))
                with self._state_lock:
                    accepted = (ready_received and "error" not in returned and time.monotonic() < deadline
                                and not self._stop_event.is_set() and not self._startup_cancelled.is_set())
                    if accepted:
                        self._startup_accepted = True
                    else:
                        self._startup_cancelled.set()
                    decision.set()
                if not accepted:
                    if ready_received and "error" in returned:
                        raise returned["error"]
                    if ready_received and self._stop_event.is_set() and time.monotonic() < deadline:
                        error = SessionCancelled("startup handoff cancelled before caller acceptance")
                        error.session = self
                        raise error
                    error = SessionTimeout(label + " exceeded timeout_seconds; in-flight startup/closure unconfirmed")
                    error.session = self
                    raise error
            # Event.wait(False) is authoritative. A coarse monotonic clock may
            # still show time remaining; a later publication cannot undo this
            # caller's rejection or commit a prepared archive snapshot.
            received = completed.wait(max(0.0, deadline - time.monotonic()))
            if not received:
                error = SessionTimeout(label + " exceeded timeout_seconds; in-flight request/closure unconfirmed")
                error.session = self
                raise error
            with self._state_lock:
                if "error" in returned:
                    raise returned["error"]
                value = returned.get("value")
                # These callbacks must perform only in-memory publication;
                # SDK/filesystem work happened before completion publication.
                if on_accept is not None:
                    value = on_accept(value, deadline)
                reservation["caller_state"] = "accepted"
                return value
        except BaseException as error:
            with self._state_lock:
                reservation["caller_state"] = ("timed_out" if isinstance(error, SessionTimeout)
                                                else "interrupted" if isinstance(error, KeyboardInterrupt)
                                                else "rejected")
                if on_reject is not None:
                    on_reject()
                if startup:
                    self._startup_cancelled.set()
                    self._startup_accepted = False
            decision.set()
            raise
        finally:
            # Completion alone cannot release admission while the caller is
            # still deciding whether its prepared result can be published.
            settled.set()

    def _startup_stopped(self, deadline):
        return self._startup_cancelled.is_set() or self._stop_event.is_set() or time.monotonic() >= deadline

    def _startup(self, interface_module, deadline):
        try:
            if self._startup_stopped(deadline):
                self._deadline(deadline)
                raise SessionCancelled("startup cancelled before vendor import")
            interface = interface_module if interface_module is not None else importlib.import_module("cst.interface")
            if self._startup_stopped(deadline):
                self._deadline(deadline)
                raise SessionCancelled("startup cancelled before new environment")
            self._check_path()
            # Only the new child environment receives this copy. Never persist
            # its potentially sensitive licensing/account environment values.
            child_environment = dict(os.environ)
            child_environment.update(TEMP=str(self._scratch_path), TMP=str(self._scratch_path))
            self._deadline(deadline)
            self._environment_creation_attempted = True
            self._de = interface.DesignEnvironment.new(env=child_environment)
            self._held_de = self._de
            pid = self._de.pid()
            observed = self._probe(pid)
            recorded = self._alive_identity(observed, pid)
            if recorded["session_id"] != self._session_id or recorded["run_dir"] != str(self.run_dir):
                raise SessionSafetyError("initial observer claimed a foreign run/session identity")
            self._identity = recorded
            if self._startup_stopped(deadline):
                self._deadline(deadline)
                raise SessionCancelled("startup cancelled after fresh environment returned")
            self._guard()
            self._deadline(deadline)
            self._project = self._de.new_mws()
            self._held_project = self._project
            if self._startup_stopped(deadline):
                self._deadline(deadline)
                raise SessionCancelled("startup cancelled after fresh project returned")
        except BaseException:
            # This executes on the existing request thread; no concurrent close.
            # If launch never returned a verifiable identity, nothing is guessed.
            self._startup_cleanup()
            raise

    def _startup_cleanup(self):
        if self._identity is None or self._startup_cleanup_attempted:
            return
        self._startup_cleanup_attempted = True
        try:
            self._last_close = self._close_body(time.monotonic() + DEFAULT_CLEANUP_SECONDS)
        except BaseException as error:
            self._last_close = self._receipt(False, "startup cleanup unconfirmed: " + str(error))

    def _alive_identity(self, observation, pid):
        if type(pid) is not int or pid <= 0 or not isinstance(observation, dict):
            raise SessionSafetyError("complete owned process identity unavailable")
        if observation.get("state", "alive") != "alive" or observation.get("pid") != pid:
            raise SessionSafetyError("owned process identity is not an observed live PID")
        created = observation.get("created_at")
        if not created or isinstance(created, bool) or not isinstance(created, (str, int, float)):
            raise SessionSafetyError("owned process creation identity missing")
        if isinstance(created, float) and not math.isfinite(created):
            raise SessionSafetyError("owned process creation identity is nonfinite")
        executable = observation.get("executable")
        if not isinstance(executable, str) or not executable or not Path(executable).is_absolute():
            raise SessionSafetyError("owned full executable path missing")
        return {"session_id": observation.get("session_id", self._session_id), "pid": pid,
                "created_at": created, "executable": executable,
                "run_dir": observation.get("run_dir", str(self.run_dir))}

    def _observe(self):
        if self._identity is None:
            return {"state": "unknown", "reason": "no recorded owned identity"}
        try:
            observed = self._probe(self._identity["pid"])
            if not isinstance(observed, dict):
                return {"state": "unknown", "reason": "identity observer returned invalid data"}
            if observed.get("state", "alive") != "alive":
                return copy.deepcopy(observed)
            return self._alive_identity(observed, self._identity["pid"])
        except Exception as error:
            return {"state": "unknown", "reason": str(error)}

    def ownership(self):
        return copy.deepcopy(self._identity), self._observe()

    def supervision_status(self):
        """Observe only the recorded PID; never assume unknown means absent.

        Pending calls (including late startup) retain their ownership obligations.
        This method issues no SDK request and cannot make cleanup concurrent.
        """
        with self._state_lock:
            reservation = self._active_request
            pending = reservation is not None and not (
                reservation["completed"].is_set() and reservation["settled"].is_set())
            creation_pending = pending and reservation["startup"]
            recorded = copy.deepcopy(self._identity)
            closed = self._owned_closed
            no_launch = self._creation_no_launch_confirmed
            launch_state = reservation["launch_state"] if reservation is not None else "not_reserved"
            caller_state = reservation["caller_state"] if reservation is not None else "not_reserved"
        created = True if recorded is not None else False if no_launch else "unknown"
        return {"request_pending": pending, "sdk_request_pending": pending,
                "session_creation_pending": creation_pending,
                "session_created": created, "owned_closed": closed,
                "request_launch_state": launch_state,
                "request_caller_state": caller_state,
                "recorded_identity": recorded, "owned_process_observation": self._observe()}

    def supervise_cleanup(self, timeout_seconds=DEFAULT_CLEANUP_SECONDS):
        """Return actual closure evidence, retaining any pending SDK request."""
        _seconds(timeout_seconds)
        if self.supervision_status()["request_pending"]:
            return self._receipt(False, "SDK request still pending; supervised cleanup deferred")
        return self.close(timeout_seconds)

    def archive_integrity(self):
        """Return cached archive diagnostics, not a new verification or rebind.

        Only verify_archive performs a complete current stable snapshot check.
        A regular replacement is not proof that CST wrote it; quarantine is
        sticky and requires a fresh run, not a caller-supplied replacement pin.
        """
        return copy.deepcopy(self._archive)

    def _quarantine_archive(self, reason, observed_identity=None):
        if self._archive["state"] != "quarantined":
            self._archive.update(state="quarantined", status="quarantined",
                                 archive_integrity_verified=False, reason=str(reason),
                                 observed_identity=observed_identity,
                                 writer_attribution="unresolved")

    def _observe_archive(self):
        # Control-plane operations do not read/hash the mutable archive. This
        # cheap observation records drift without granting data-plane access.
        if not self._saved:
            return
        self._archive["archive_integrity_verified"] = False
        try:
            actual = self._file_identity()
            if actual != self._saved_file_identity:
                self._quarantine_archive("owned saved project file was replaced", list(actual))
        except (OSError, SessionSafetyError) as error:
            self._quarantine_archive(str(error))

    def _check_path(self):
        if self._run_input.resolve() != self.run_dir or not self.run_dir.is_dir():
            raise SessionSafetyError("owned run path changed")
        if any(_linked(path) for path in (self._run_input, *self._run_input.parents,
                                         self._project_path, self._scratch_path)):
            raise SessionSafetyError("owned project/run path traverses a symlink or junction")
        if self._project_path.resolve().parent != self.run_dir:
            raise SessionSafetyError("project path escapes exact owned run")
        if self._scratch_path.resolve().parent != self.run_dir:
            raise SessionSafetyError("scratch path escapes exact owned run")

    def _require_held_objects(self, environment, project=None, *, allow_project_closed=False):
        if environment is None or self._de is not environment or self._held_de is not environment:
            raise SessionSafetyError("SDK environment object is not the captured held owned environment")
        if project is not None and (self._project is not project or self._held_project is not project or
                                    self._project_closed and not allow_project_closed):
            raise SessionSafetyError("SDK project object is not the captured held owned project")

    def _guard(self, *, allow_cancelled=False):
        environment = self._held_de
        project = self._held_project
        recorded = copy.deepcopy(self._identity)
        self._require_held_objects(environment)
        if self._project is not project:
            raise SessionSafetyError("SDK project reference is not the captured held project")
        self._check_path()
        if self._owned_closed or self._de_close_returned:
            raise SessionSafetyError("environment already closed or awaiting closure confirmation")
        if not allow_cancelled and self._stop_event.is_set():
            raise SessionCancelled("owned session stop_event is set")
        observed = self._observe()
        self._require_held_objects(environment)
        if self._project is not project or self._held_project is not project:
            raise SessionSafetyError("SDK project reference changed during ownership observation")
        if observed.get("state", "alive") != "alive" or recorded is None or self._identity != recorded or any(
                observed.get(key) != recorded.get(key) for key in IDENTITY_KEYS):
            raise SessionSafetyError("complete process identity no longer matches owned session")
        if not allow_cancelled and self._stop_event.is_set():
            raise SessionCancelled("owned session stop_event was set during identity observation")
        return environment

    def _project_guard(self, *, allow_cancelled=False, require_saved=False,
                       archive_required=True, deadline=None):
        project = self._held_project
        environment = self._guard(allow_cancelled=allow_cancelled)
        if project is None:
            raise SessionSafetyError("owned project is unavailable or closed")
        self._require_held_objects(environment, project)
        if require_saved and not self._saved:
            raise SessionSafetyError("owned project must be saved before project close")
        if self._saved or self._project_save_attempted:
            filename = project.filename()
            self._require_held_objects(environment, project)
            empty_fresh = filename == "" and not self._saved and not self._project_location_bound
            exact_owned = isinstance(filename, str) and bool(filename) and Path(filename).resolve() == self.project_path
            if not empty_fresh and not exact_owned:
                raise SessionSafetyError("SDK project filename is not exact owned saved path")
            if exact_owned:
                # A save may have established this location even though its
                # archive snapshot was never accepted by the caller.
                self._project_location_bound = True
            if archive_required and self._saved:
                self._verify_archive_body(deadline, allow_cancelled=allow_cancelled)
            elif not archive_required:
                self._observe_archive()
        # Snapshot/filename observations can block. Recheck the held process
        # before handing the project to any following SDK action.
        self._guard(allow_cancelled=allow_cancelled)
        self._require_held_objects(environment, project)
        return project

    def _file_identity(self):
        info = self.project_path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not info.st_ino:
            raise SessionSafetyError("saved project must be one regular file without hardlink aliases")
        return info.st_dev, info.st_ino

    def _archive_snapshot(self, deadline, *, allow_cancelled=False):
        """Bound a regular, single-link stable snapshot; no writer attribution.

        SHA256 also detects in-place changes that preserve the archive inode.
        The pathname SDK still has a TOCTOU boundary; this is not a handle-based
        guarantee against an adversarial writer racing an authorized SDK save.
        """
        self._check_path()
        identity = self._file_identity()
        digest = hashlib.sha256()
        with self.project_path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino) != identity or before.st_nlink != 1:
                raise SessionSafetyError("archive changed while opening stable snapshot")
            while True:
                if deadline is not None:
                    self._deadline(deadline, allow_cancelled=allow_cancelled)
                data = stream.read(1024 * 1024)
                if not data:
                    break
                digest.update(data)
            after = os.fstat(stream.fileno())
        self._check_path()
        current = self.project_path.stat()
        keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_nlink")
        if any(getattr(before, key) != getattr(after, key) or
               getattr(after, key) != getattr(current, key) for key in keys):
            raise SessionSafetyError("archive changed during stable snapshot")
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
            raise SessionSafetyError("archive snapshot is not one regular unaliased file")
        return identity, digest.hexdigest()

    def _verify_archive_body(self, deadline, *, allow_cancelled=False):
        if self._archive["state"] == "quarantined":
            raise SessionSafetyError("owned archive quarantined: " + self._archive["reason"])
        try:
            identity, digest = self._archive_snapshot(deadline, allow_cancelled=allow_cancelled)
            if identity != self._saved_file_identity:
                raise SessionSafetyError("owned saved project file was replaced")
            if digest != self._saved_file_digest:
                raise SessionSafetyError("owned saved project content changed without an explicit checked save")
        except SessionCancelled:
            raise
        except (OSError, SessionSafetyError) as error:
            self._quarantine_archive(str(error))
            raise SessionSafetyError("owned archive quarantined: " + str(error)) from error
        self._archive["archive_integrity_verified"] = True
        self._archive["verification_scope"] = "last_successful_strict_snapshot"
        return self.archive_integrity()

    def verify_archive(self, timeout_seconds=DEFAULT_REQUEST_SECONDS):
        """Strict gate for save/export; a solver-time inode is never rebound."""
        def verify(deadline):
            if self._project_closed:
                # Results are read only after our documented without-saving
                # project close. Do not query that closed SDK object.
                self._guard()
                if not self._saved or self._project is not self._held_project:
                    raise SessionSafetyError("no held saved archive is available for export verification")
                self._verify_archive_body(deadline)
                self._guard()
            else:
                self._project_guard(require_saved=True, deadline=deadline)
            self._deadline(deadline)
            return self.archive_integrity()
        return self._request("verify-archive", verify, timeout_seconds)

    def _deadline(self, deadline, *, allow_cancelled=False):
        if time.monotonic() >= deadline:
            raise SessionTimeout("owned SDK operation exceeded seconds deadline")
        reservation = self._active_request
        if not allow_cancelled and reservation is not None and not reservation["completed"].is_set():
            if reservation["caller_state"] == "timed_out":
                raise SessionTimeout("SDK caller already timed out; late work cannot be accepted")
            if reservation["caller_state"] in ("interrupted", "rejected"):
                raise SessionCancelled("SDK caller left; deferred work cannot be accepted")
        if not allow_cancelled and (self._stop_event.is_set() or self._startup_cancelled.is_set()):
            raise SessionCancelled("owned operation cancelled before SDK mutation")

    def save(self, include_results=True, timeout_seconds=DEFAULT_REQUEST_SECONDS):
        if type(include_results) is not bool:
            raise SessionSafetyError("include_results must be boolean")
        old_identity, old_digest, old_saved = self._saved_file_identity, self._saved_file_digest, self._saved
        def save_owned(deadline):
            environment = self._held_de
            project = self._project_guard(deadline=deadline)
            self._require_held_objects(environment, project)
            if not self._saved and self.project_path.exists():
                raise FileExistsError("initial owned project target now exists")
            self._deadline(deadline)
            self._archive["archive_integrity_verified"] = False
            try:
                self._project_save_attempted = True
                project.save(str(self.project_path), include_results=include_results, allow_overwrite=self._saved)
                # No timeout/failed SDK request may commit a new archive pin.
                # Only the explicit synchronous save is a writer trust boundary.
                self._deadline(deadline)
                self._guard()
                self._require_held_objects(environment, project)
                filename = project.filename()
                self._deadline(deadline)
                self._require_held_objects(environment, project)
                if Path(filename).resolve() != self.project_path:
                    raise SessionSafetyError("save did not establish the exact owned project file")
                identity, digest = self._archive_snapshot(deadline)
                self._deadline(deadline)
                self._guard()
                self._require_held_objects(environment, project)
                filename = project.filename()
                self._deadline(deadline)
                self._require_held_objects(environment, project)
                if Path(filename).resolve() != self.project_path:
                    raise SessionSafetyError("SDK filename changed before archive pin commit")
                # Filename/observer callbacks can block or replace Python-side
                # references. Validate after the final callback and retain the
                # captured SDK objects; no mutable target is fetched for use.
                self._guard()
                self._require_held_objects(environment, project)
                self._deadline(deadline)
                if self._file_identity() != identity:
                    raise SessionSafetyError("archive identity changed before pin commit")
                self._require_held_objects(environment, project)
                self._deadline(deadline)
            except BaseException:
                self._quarantine_archive("explicit SDK save failed or exceeded its deadline; new archive was not accepted")
                raise
            # Preparation is not publication. The caller commits this snapshot
            # only after receiving conclusively completed work in its budget.
            return environment, project, identity, digest
        def accept(prepared, deadline):
            environment, project, identity, digest = prepared
            archive = {"state": "pinned", "status": "pinned",
                       "archive_integrity_verified": True,
                       "pin_operation": "save", "pinned_identity": list(identity),
                       "sha256": digest,
                       "verification_scope": "last_successful_strict_snapshot",
                       "writer_attribution": "explicit_sdk_save_trust_boundary"}
            self._require_held_objects(environment, project)
            self._deadline(deadline)
            self._saved_file_identity, self._saved_file_digest = identity, digest
            self._saved, self._archive = True, archive
        def reject():
            self._saved_file_identity, self._saved_file_digest, self._saved = old_identity, old_digest, old_saved
            self._quarantine_archive("explicit SDK save was not accepted by its caller; new archive was not committed")
        return self._request("save", save_owned, timeout_seconds, on_accept=accept, on_reject=reject)

    def add_to_history(self, header, code, timeout_seconds=DEFAULT_REQUEST_SECONDS):
        def apply(deadline):
            project = self._project_guard(deadline=deadline)
            self._deadline(deadline)
            return project.model3d.add_to_history(header, code)
        return self._request("history", apply, timeout_seconds)

    def execute_vba(self, code, timeout_seconds=DEFAULT_REQUEST_SECONDS):
        def execute(deadline):
            project = self._project_guard(deadline=deadline)
            self._deadline(deadline)
            return project.schematic.execute_vba_code(code)
        return self._request("vba", execute, timeout_seconds)

    def execute_readback(self, *, kind, timeout_seconds=DEFAULT_REQUEST_SECONDS):
        """Run only the original fixed mesh query on the exact held project.

        No caller VBA/path is accepted, and this does not authorize archive
        save/export. Reference-plane and CPU getter support remains unresolved.
        """
        if kind != "mesh":
            raise SessionSafetyError("unsupported trusted readback kind; only documented mesh queries are available")
        def read(deadline):
            from . import native_model
            project = self._project_guard(allow_cancelled=True, require_saved=True,
                                          archive_required=False, deadline=deadline)
            target = self.run_dir / "native_mesh.tsv"
            if _linked(target) or target.exists() and target not in self._readback_outputs:
                raise SessionSafetyError("trusted readback destination is linked or preexisting/unowned")
            if target in self._readback_outputs:
                info = target.stat()
                if (info.st_dev, info.st_ino) != self._readback_outputs[target] or info.st_nlink != 1:
                    raise SessionSafetyError("owned readback destination was replaced or aliased")
            code = native_model.mesh_readback_vba(self.run_dir)
            self._deadline(deadline, allow_cancelled=True)
            result = project.schematic.execute_vba_code(code)
            self._project_guard(allow_cancelled=True, require_saved=True,
                                archive_required=False, deadline=deadline)
            self._deadline(deadline, allow_cancelled=True)
            if _linked(target):
                raise SessionSafetyError("readback destination became linked")
            info = target.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SessionSafetyError("readback output is not one regular unaliased owned file")
            self._readback_outputs[target] = (info.st_dev, info.st_ino)
            return {"kind": kind, "sdk_return": result, "archive_integrity": self.archive_integrity()}
        return self._request("trusted-readback-" + kind, read, timeout_seconds)

    def start_solver(self, timeout_seconds=DEFAULT_REQUEST_SECONDS):
        def start(deadline):
            project = self._project_guard(require_saved=True, deadline=deadline)
            self._deadline(deadline)
            return project.model3d.start_solver()
        return self._request("start", start, timeout_seconds)

    def _running(self, deadline):
        project = self._project_guard(allow_cancelled=True, archive_required=False, deadline=deadline)
        self._deadline(deadline, allow_cancelled=True)
        running = project.model3d.is_solver_running()
        if type(running) is not bool:
            raise SessionSafetyError("SDK solver-running query did not return a boolean")
        return running

    def is_solver_running(self, timeout_seconds=DEFAULT_REQUEST_SECONDS):
        return self._request("poll", self._running, timeout_seconds)

    def abort_solver(self, timeout_seconds=DEFAULT_REQUEST_SECONDS):
        def abort(deadline):
            project = self._project_guard(allow_cancelled=True, archive_required=False, deadline=deadline)
            self._deadline(deadline, allow_cancelled=True)
            return project.model3d.abort_solver()
        return self._request("abort", abort, timeout_seconds)

    def get_solver_run_info(self, timeout_seconds=DEFAULT_REQUEST_SECONDS):
        def read(deadline):
            project = self._project_guard(allow_cancelled=True, archive_required=False, deadline=deadline)
            self._deadline(deadline, allow_cancelled=True)
            info = project.model3d.get_solver_run_info()
            if not isinstance(info, dict):
                raise SessionSafetyError("SDK solver run information did not return a dict")
            return copy.deepcopy(info)
        return self._request("solver-info", read, timeout_seconds)

    def _close_project_body(self, deadline):
        project = self._project_guard(allow_cancelled=True,
                                      archive_required=False, deadline=deadline)
        if not self._saved and not self._project_location_bound:
            raise SessionSafetyError("owned project must have an exact saved location before project close")
        if self._running(deadline):
            raise SessionSafetyError("owned solver must be stopped before project close")
        self._project_guard(allow_cancelled=True,
                            archive_required=False, deadline=deadline)
        self._deadline(deadline, allow_cancelled=True)
        project.close()
        self._project_closed = True

    def close_project(self, timeout_seconds=DEFAULT_REQUEST_SECONDS):
        return self._request("project-close", self._close_project_body, timeout_seconds)

    def _original_gone(self, observation):
        if self._identity is None or observation.get("pid") != self._identity["pid"]:
            return False
        if observation.get("state") == "gone":
            return True
        # A complete newly created process at this PID proves original reuse.
        return observation.get("state", "alive") == "alive" and all(
            key in observation for key in IDENTITY_KEYS) and observation["created_at"] != self._identity["created_at"]

    def _close_body(self, deadline):
        if self._owned_closed:
            return copy.deepcopy(self._confirmed_close)
        if self._identity is None or self._de is None:
            return self._receipt(False, "fresh environment identity unavailable; startup/closure unconfirmed")
        environment, held_project = self._held_de, self._held_project
        observed = self._observe()
        if self._original_gone(observed):
            self._owned_closed = True
            self._confirmed_close = self._receipt(True, "exact original process already gone; no SDK action issued",
                                                 closure_kind="original_process_already_gone",
                                                 process_observation=observed,
                                                 archive_integrity=self.archive_integrity())
            return copy.deepcopy(self._confirmed_close)
        if not self._de_close_returned:
            self._guard(allow_cancelled=True)
            self._require_held_objects(environment)
            if self._project is not held_project:
                raise SessionSafetyError("project reference changed during environment close")
            if self._project is not None and not self._project_closed:
                if self._running(deadline):
                    project = self._project_guard(allow_cancelled=True, archive_required=False, deadline=deadline)
                    self._deadline(deadline, allow_cancelled=True)
                    project.model3d.abort_solver()
                    while self._running(deadline):
                        self._deadline(deadline, allow_cancelled=True)
                        time.sleep(min(.01, max(0, deadline - time.monotonic())))
                # Project.close explicitly requires this saved owned project.
                # A never-saved fresh project is discarded only via its own DE.
                if self._saved or self._project_location_bound:
                    self._close_project_body(deadline)
            self._guard(allow_cancelled=True)
            self._require_held_objects(environment, held_project,
                                       allow_project_closed=self._project_closed)
            self._deadline(deadline, allow_cancelled=True)
            environment.close()
            self._de_close_returned = True
        while True:
            observed = self._observe()
            if self._original_gone(observed):
                self._owned_closed = True
                self._confirmed_close = self._receipt(True, "original owned process gone or PID reused",
                                                     closure_kind="owned_sdk_close_then_original_gone",
                                                     process_observation=observed,
                                                     archive_integrity=self.archive_integrity())
                return copy.deepcopy(self._confirmed_close)
            if time.monotonic() >= deadline:
                return self._receipt(False, "original process exit not confirmed", process_observation=observed)
            time.sleep(min(.01, max(0, deadline - time.monotonic())))

    def close(self, timeout_seconds=DEFAULT_REQUEST_SECONDS):
        _seconds(timeout_seconds)
        try:
            self._last_close = self._request("environment-close", self._close_body, timeout_seconds)
        except Exception as error:
            self._last_close = self._receipt(False, "owned cleanup unconfirmed: " + str(error))
        return copy.deepcopy(self._last_close)

    def cleanup_identity(self, recorded):
        if not isinstance(recorded, dict) or recorded != self._identity:
            return self._receipt(False, "cleanup identity does not match this recorded session")
        return self.close(DEFAULT_CLEANUP_SECONDS)
