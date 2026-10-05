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
"""
from __future__ import annotations

import copy
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
        self._de = self._project = None
        self._identity = None
        self._saved = self._project_closed = self._de_close_returned = False
        self._saved_file_identity = None
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

    def _request(self, label, function, timeout_seconds, *, startup=False):
        deadline = time.monotonic() + _seconds(timeout_seconds)
        returned = {}
        ready = threading.Event()
        decision = threading.Event()
        def invoke():
            try:
                returned["value"] = function(deadline)
            except BaseException as error:
                returned["error"] = error
            finally:
                if startup:
                    # The caller atomically accepts the completed startup or
                    # cancels it. Until that decision, this worker retains the
                    # freshly returned objects and is solely responsible for
                    # late cleanup. Completion cannot race a final check.
                    ready.set()
                    decision.wait(max(0.0, deadline - time.monotonic()))
                    with self._state_lock:
                        accepted = self._startup_accepted
                        if not accepted:
                            self._startup_cancelled.set()
                    if not accepted:
                        self._startup_cleanup()
        thread = threading.Thread(target=invoke, daemon=True, name="owned-cst-" + label)
        with self._state_lock:
            if self._active_thread is not None and self._active_thread.is_alive():
                raise SessionSafetyError("SDK request still pending; concurrent SDK actions are forbidden")
            self._active_thread = thread
            thread.start()
        if startup:
            ready.wait(max(0.0, deadline - time.monotonic()))
            with self._state_lock:
                accepted = (ready.is_set() and "error" not in returned and time.monotonic() < deadline
                            and not self._stop_event.is_set() and not self._startup_cancelled.is_set())
                if accepted:
                    self._startup_accepted = True
                    # The old worker has finished every SDK operation. Its
                    # remaining accepted-handoff bookkeeping performs none.
                    self._active_thread = None
                else:
                    self._startup_cancelled.set()
                decision.set()
            if accepted:
                return returned.get("value")
            if ready.is_set() and "error" in returned:
                raise returned["error"]
            if ready.is_set() and self._stop_event.is_set() and time.monotonic() < deadline:
                error = SessionCancelled("startup handoff cancelled before caller acceptance")
                error.session = self
                raise error
            error = SessionTimeout(label + " exceeded timeout_seconds; in-flight startup/closure unconfirmed")
            error.session = self
            raise error
        thread.join(max(0.0, deadline - time.monotonic()))
        if thread.is_alive():
            error = SessionTimeout(label + " exceeded timeout_seconds; in-flight request/closure unconfirmed")
            error.session = self
            raise error
        if "error" in returned:
            raise returned["error"]
        return returned.get("value")

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
            self._de = interface.DesignEnvironment.new(env=child_environment)
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

    def _guard(self, *, allow_cancelled=False):
        self._check_path()
        if self._owned_closed or self._de_close_returned:
            raise SessionSafetyError("environment already closed or awaiting closure confirmation")
        if not allow_cancelled and self._stop_event.is_set():
            raise SessionCancelled("owned session stop_event is set")
        observed = self._observe()
        if observed.get("state", "alive") != "alive" or self._identity is None or any(
                observed.get(key) != self._identity.get(key) for key in IDENTITY_KEYS):
            raise SessionSafetyError("complete process identity no longer matches owned session")
        if not allow_cancelled and self._stop_event.is_set():
            raise SessionCancelled("owned session stop_event was set during identity observation")

    def _project_guard(self, *, allow_cancelled=False, require_saved=False):
        self._guard(allow_cancelled=allow_cancelled)
        if self._project is None or self._project_closed:
            raise SessionSafetyError("owned project is unavailable or closed")
        if require_saved and not self._saved:
            raise SessionSafetyError("owned project must be saved before project close")
        if self._saved:
            filename = self._project.filename()
            if not isinstance(filename, str) or not filename or Path(filename).resolve() != self.project_path:
                raise SessionSafetyError("SDK project filename is not exact owned saved path")
            if self._file_identity() != self._saved_file_identity:
                raise SessionSafetyError("owned saved project file was replaced")
        return self._project

    def _file_identity(self):
        info = self.project_path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not info.st_ino:
            raise SessionSafetyError("saved project must be one regular file without hardlink aliases")
        return info.st_dev, info.st_ino

    def _deadline(self, deadline, *, allow_cancelled=False):
        if time.monotonic() >= deadline:
            raise SessionTimeout("owned SDK operation exceeded seconds deadline")
        if not allow_cancelled and (self._stop_event.is_set() or self._startup_cancelled.is_set()):
            raise SessionCancelled("owned operation cancelled before SDK mutation")

    def save(self, include_results=True, timeout_seconds=DEFAULT_REQUEST_SECONDS):
        if type(include_results) is not bool:
            raise SessionSafetyError("include_results must be boolean")
        def save_owned(deadline):
            project = self._project_guard()
            if not self._saved and self.project_path.exists():
                raise FileExistsError("initial owned project target now exists")
            self._deadline(deadline)
            project.save(str(self.project_path), include_results=include_results, allow_overwrite=self._saved)
            self._check_path()
            if Path(project.filename()).resolve() != self.project_path or not self.project_path.is_file():
                raise SessionSafetyError("save did not establish the exact owned project file")
            self._saved_file_identity = self._file_identity()
            self._saved = True
        return self._request("save", save_owned, timeout_seconds)

    def add_to_history(self, header, code, timeout_seconds=DEFAULT_REQUEST_SECONDS):
        def apply(deadline):
            project = self._project_guard()
            self._deadline(deadline)
            return project.model3d.add_to_history(header, code)
        return self._request("history", apply, timeout_seconds)

    def execute_vba(self, code, timeout_seconds=DEFAULT_REQUEST_SECONDS):
        def execute(deadline):
            project = self._project_guard()
            self._deadline(deadline)
            return project.schematic.execute_vba_code(code)
        return self._request("vba", execute, timeout_seconds)

    def start_solver(self, timeout_seconds=DEFAULT_REQUEST_SECONDS):
        def start(deadline):
            project = self._project_guard(require_saved=True)
            self._deadline(deadline)
            return project.model3d.start_solver()
        return self._request("start", start, timeout_seconds)

    def _running(self, deadline):
        project = self._project_guard(allow_cancelled=True)
        self._deadline(deadline, allow_cancelled=True)
        running = project.model3d.is_solver_running()
        if type(running) is not bool:
            raise SessionSafetyError("SDK solver-running query did not return a boolean")
        return running

    def is_solver_running(self, timeout_seconds=DEFAULT_REQUEST_SECONDS):
        return self._request("poll", self._running, timeout_seconds)

    def abort_solver(self, timeout_seconds=DEFAULT_REQUEST_SECONDS):
        def abort(deadline):
            project = self._project_guard(allow_cancelled=True)
            self._deadline(deadline, allow_cancelled=True)
            return project.model3d.abort_solver()
        return self._request("abort", abort, timeout_seconds)

    def get_solver_run_info(self, timeout_seconds=DEFAULT_REQUEST_SECONDS):
        def read(deadline):
            project = self._project_guard(allow_cancelled=True)
            self._deadline(deadline, allow_cancelled=True)
            info = project.model3d.get_solver_run_info()
            if not isinstance(info, dict):
                raise SessionSafetyError("SDK solver run information did not return a dict")
            return copy.deepcopy(info)
        return self._request("solver-info", read, timeout_seconds)

    def _close_project_body(self, deadline):
        project = self._project_guard(allow_cancelled=True, require_saved=True)
        if self._running(deadline):
            raise SessionSafetyError("owned solver must be stopped before project close")
        self._project_guard(allow_cancelled=True, require_saved=True)
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
        if not self._de_close_returned:
            self._guard(allow_cancelled=True)
            if self._project is not None and not self._project_closed:
                if self._running(deadline):
                    project = self._project_guard(allow_cancelled=True)
                    self._deadline(deadline, allow_cancelled=True)
                    project.model3d.abort_solver()
                    while self._running(deadline):
                        self._deadline(deadline, allow_cancelled=True)
                        time.sleep(min(.01, max(0, deadline - time.monotonic())))
                # Project.close explicitly requires this saved owned project.
                # A never-saved fresh project is discarded only via its own DE.
                if self._saved:
                    self._close_project_body(deadline)
            self._guard(allow_cancelled=True)
            self._deadline(deadline, allow_cancelled=True)
            self._de.close()
            self._de_close_returned = True
        while True:
            observed = self._observe()
            if self._original_gone(observed):
                self._owned_closed = True
                self._confirmed_close = self._receipt(True, "original owned process gone or PID reused",
                                                     process_observation=observed)
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
