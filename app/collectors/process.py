"""Process table collector.

Performance is the whole design problem here.  A desktop runs 300-500 processes;
naively asking psutil for twenty attributes each means thousands of ``/proc``
reads per poll, which is exactly why some monitors visibly stutter.

Three techniques keep this cheap:

1. **Batch attribute fetch.**  ``Process.as_dict(attrs=...)`` reads
   ``/proc/<pid>/stat`` and ``status`` once and satisfies many attributes from
   that single read.
2. **Persistent process objects.**  psutil caches per-process state, and
   ``cpu_percent()`` is only meaningful as a delta between calls on the *same*
   object.  Recreating objects each poll would make every process report either
   zero or its lifetime average.
3. **Cheap/expensive split.**  Open files, threads, environment and connections
   are only fetched for a single process on demand by the inspector.
"""

from __future__ import annotations

import contextlib
import os
import time

import psutil

from app.collectors.base import Collector
from app.models.base import Reason, Unavailable
from app.models.process import (
    OpenFile,
    ProcessInfo,
    ProcessSnapshot,
    ProcessState,
    ThreadInfo,
)

#: Attributes satisfied by one or two ``/proc`` reads per process.
_BULK_ATTRS = (
    "pid", "name", "status", "ppid", "username", "num_threads", "nice",
    "memory_info", "memory_percent", "create_time", "cpu_times",
)

#: Number of logical CPUs, used to normalise psutil's per-core percentages.
_CPU_COUNT = psutil.cpu_count(logical=True) or 1


class ProcessCollector(Collector[ProcessSnapshot]):
    """Enumerates the process table efficiently."""

    domain = "processes"
    title = "Processes"

    def __init__(self, capabilities, hwmon=None) -> None:
        super().__init__(capabilities, hwmon)
        #: Persistent psutil objects keyed by PID, required for correct CPU
        #: percentages and for psutil's own internal caching to pay off.
        self._tracked: dict[int, psutil.Process] = {}
        self._cmdline_cache: dict[int, str] = {}
        self._exe_cache: dict[int, str] = {}
        self._io_seen: dict[int, tuple[int, int]] = {}
        #: When true, divide CPU percentages by the core count so that the column
        #: sums to 100% across the machine rather than to 100% per core.
        self.normalise_cpu = True
        #: PID-to-GPU-memory map, injected by the monitor service from the GPU
        #: snapshot so the process table can show GPU usage without re-querying.
        self._gpu_memory: dict[int, int] = {}

    def set_gpu_memory(self, mapping: dict[int, int]) -> None:
        """Supply GPU memory per PID, sourced from the GPU collector.

        Cross-domain enrichment is pushed in rather than pulled, so this
        collector keeps no dependency on the GPU layer and remains testable in
        isolation.
        """
        self._gpu_memory = dict(mapping)

    # ---------------------------------------------------------------- collection

    def _collect(self) -> ProcessSnapshot:
        now = time.monotonic()
        wall_clock = time.time()
        current: dict[int, psutil.Process] = {}
        processes: list[ProcessInfo] = []
        tally = dict.fromkeys(ProcessState, 0)
        threads_total = 0
        restricted = 0

        for pid in psutil.pids():
            proc = self._tracked.get(pid)
            if proc is None:
                try:
                    proc = psutil.Process(pid)
                except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                    continue
                # Seed cpu_percent so the next poll yields a real delta rather
                # than the process's lifetime average.
                with contextlib.suppress(psutil.Error, OSError):
                    proc.cpu_percent(interval=None)
            current[pid] = proc

            info = self._read_process(proc, now, wall_clock)
            if info is None:
                continue
            processes.append(info)
            tally[info.state] = tally.get(info.state, 0) + 1
            threads_total += info.num_threads
            if info.restricted:
                restricted += 1

        # Drop bookkeeping for processes that have exited, so the caches cannot
        # grow without bound over a long session.
        departed = set(self._tracked) - set(current)
        for pid in departed:
            self._cmdline_cache.pop(pid, None)
            self._exe_cache.pop(pid, None)
            self._io_seen.pop(pid, None)
            self.rates.forget(f"proc.{pid}.")
        self._tracked = current

        return ProcessSnapshot(
            timestamp=now,
            processes=tuple(processes),
            total=len(processes),
            running=tally.get(ProcessState.RUNNING, 0),
            sleeping=tally.get(ProcessState.SLEEPING, 0),
            stopped=tally.get(ProcessState.STOPPED, 0),
            zombie=tally.get(ProcessState.ZOMBIE, 0),
            threads=threads_total,
            restricted_count=restricted,
        )

    def _empty(self) -> ProcessSnapshot:
        return ProcessSnapshot()

    def _read_process(
        self, proc: psutil.Process, now: float, wall_clock: float
    ) -> ProcessInfo | None:
        """Read the cheap attribute set for one process."""
        pid = proc.pid
        try:
            data = proc.as_dict(attrs=_BULK_ATTRS, ad_value=None)
        except (psutil.NoSuchProcess, OSError):
            return None
        except psutil.AccessDenied:
            data = {"pid": pid, "name": f"pid {pid}"}

        restricted = False
        try:
            cpu = proc.cpu_percent(interval=None)
        except (psutil.Error, OSError):
            cpu = 0.0
        if self.normalise_cpu:
            cpu = cpu / _CPU_COUNT

        memory = data.get("memory_info")
        rss = getattr(memory, "rss", 0) or 0
        vms = getattr(memory, "vms", 0) or 0

        create_time = data.get("create_time")
        runtime = (wall_clock - create_time) if create_time else None

        cpu_times = data.get("cpu_times")
        cpu_time = (
            (cpu_times.user + cpu_times.system) if cpu_times is not None else None
        )

        read_rate, write_rate = self._io_rates(proc, now)
        if read_rate is None:
            restricted = True

        return ProcessInfo(
            pid=pid,
            name=data.get("name") or f"pid {pid}",
            state=ProcessState.from_psutil(data.get("status") or ""),
            cpu_percent=cpu,
            memory_rss_bytes=rss,
            memory_vms_bytes=vms,
            memory_percent=data.get("memory_percent") or 0.0,
            ppid=data.get("ppid"),
            username=data.get("username") or Unavailable(
                Reason.PERMISSION, "owner not readable"
            ),
            num_threads=data.get("num_threads") or 1,
            nice=data.get("nice"),
            create_time=create_time,
            runtime=runtime,
            cpu_time=cpu_time,
            exe=self._exe(proc),
            cmdline=self._cmdline(proc),
            io_read_bytes_per_s=read_rate if read_rate is not None else Unavailable(
                Reason.PERMISSION, "per-process I/O requires matching privileges"
            ),
            io_write_bytes_per_s=write_rate if write_rate is not None else Unavailable(
                Reason.PERMISSION, "per-process I/O requires matching privileges"
            ),
            gpu_memory_bytes=self._gpu_memory.get(pid),
            restricted=restricted,
        )

    # --------------------------------------------------------------- attributes

    def _cmdline(self, proc: psutil.Process):
        """Command line, cached because it is immutable after ``exec``."""
        pid = proc.pid
        if pid in self._cmdline_cache:
            return self._cmdline_cache[pid]
        try:
            parts = proc.cmdline()
        except psutil.AccessDenied:
            return Unavailable(Reason.PERMISSION, "command line not readable")
        except (psutil.NoSuchProcess, OSError):
            return Unavailable(Reason.ERROR, "process exited")
        value = " ".join(parts)
        self._cmdline_cache[pid] = value
        return value

    def _exe(self, proc: psutil.Process):
        """Executable path, cached for the process's lifetime."""
        pid = proc.pid
        if pid in self._exe_cache:
            return self._exe_cache[pid]
        try:
            value = proc.exe()
        except psutil.AccessDenied:
            return Unavailable(Reason.PERMISSION, "executable path not readable")
        except (psutil.NoSuchProcess, OSError):
            return Unavailable(Reason.ERROR, "process exited")
        self._exe_cache[pid] = value
        return value

    def _io_rates(
        self, proc: psutil.Process, now: float
    ) -> tuple[float | None, float | None]:
        """Per-process I/O throughput in bytes per second.

        ``/proc/<pid>/io`` is readable only by the process owner (or root), so
        this legitimately fails for most processes on a normal desktop.  The
        ``None`` return is translated into an explanatory ``Unavailable`` rather
        than a zero, so the user is not misled into thinking a busy process is
        idle.
        """
        try:
            counters = proc.io_counters()
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError, AttributeError):
            return None, None
        pid = proc.pid
        return (
            self.rates.rate(f"proc.{pid}.r", counters.read_bytes, now),
            self.rates.rate(f"proc.{pid}.w", counters.write_bytes, now),
        )

    # ---------------------------------------------------------------- inspector

    def inspect(self, pid: int) -> ProcessInfo | None:
        """Read the full, expensive attribute set for one process.

        Called only when the user selects a process, because enumerating threads,
        file descriptors and environment for every process would cost hundreds of
        additional syscalls per poll.
        """
        proc = self._tracked.get(pid)
        if proc is None:
            try:
                proc = psutil.Process(pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                return None
        base = self._read_process(proc, time.monotonic(), time.time())
        if base is None:
            return None

        from dataclasses import replace

        return replace(
            base,
            threads=self._threads(proc),
            open_files=self._open_files(proc),
            environment=self._environment(proc),
            cwd=self._cwd(proc),
            cpu_affinity=self._affinity(proc),
            num_fds=self._num_fds(proc),
            connections=self._connection_count(proc),
            priority=self._priority(proc),
            num_ctx_switches=self._context_switches(proc),
        )

    @staticmethod
    def _threads(proc: psutil.Process) -> tuple[ThreadInfo, ...]:
        """Per-thread CPU times."""
        try:
            return tuple(
                ThreadInfo(
                    tid=t.id, user_time=t.user_time, system_time=t.system_time
                )
                for t in proc.threads()
            )
        except (psutil.Error, OSError):
            return ()

    @staticmethod
    def _open_files(proc: psutil.Process) -> tuple[OpenFile, ...]:
        """Open regular-file descriptors."""
        try:
            return tuple(
                OpenFile(path=f.path, fd=f.fd, mode=getattr(f, "mode", None))
                for f in proc.open_files()
            )
        except (psutil.Error, OSError):
            return ()

    def _environment(self, proc: psutil.Process) -> dict[str, str]:
        """Environment variables, with obvious secrets redacted.

        The environment of a process routinely contains API tokens and
        passwords.  Showing them verbatim in a monitoring UI -- which a user may
        well screen-share -- would be irresponsible, so keys whose names suggest
        a credential have their values masked.
        """
        try:
            raw = proc.environ()
        except (psutil.Error, OSError):
            return {}
        sensitive = ("token", "secret", "password", "passwd", "key", "credential",
                     "auth", "session", "cookie", "private")
        redacted: dict[str, str] = {}
        for key, value in raw.items():
            if any(marker in key.lower() for marker in sensitive):
                redacted[key] = f"<redacted, {len(value)} characters>"
            else:
                redacted[key] = value
        return redacted

    @staticmethod
    def _cwd(proc: psutil.Process):
        """Working directory."""
        try:
            return proc.cwd()
        except psutil.AccessDenied:
            return Unavailable(Reason.PERMISSION, "working directory not readable")
        except (psutil.Error, OSError):
            return Unavailable(Reason.ERROR, "unavailable")

    @staticmethod
    def _affinity(proc: psutil.Process) -> tuple[int, ...]:
        """CPUs this process is permitted to run on."""
        try:
            return tuple(proc.cpu_affinity())
        except (psutil.Error, OSError, AttributeError):
            return ()

    @staticmethod
    def _num_fds(proc: psutil.Process):
        """Count of open file descriptors."""
        try:
            return proc.num_fds()
        except (psutil.Error, OSError, AttributeError):
            return Unavailable(Reason.PERMISSION, "descriptor count not readable")

    @staticmethod
    def _connection_count(proc: psutil.Process) -> int:
        """Number of network connections held by this process."""
        try:
            return len(proc.net_connections(kind="inet"))
        except (psutil.Error, OSError, AttributeError):
            return 0

    @staticmethod
    def _priority(proc: psutil.Process):
        """Scheduling priority."""
        try:
            return proc.nice()
        except (psutil.Error, OSError):
            return Unavailable(Reason.PERMISSION, "priority not readable")

    @staticmethod
    def _context_switches(proc: psutil.Process):
        """Total voluntary and involuntary context switches."""
        try:
            switches = proc.num_ctx_switches()
            return switches.voluntary + switches.involuntary
        except (psutil.Error, OSError, AttributeError):
            return Unavailable(Reason.PERMISSION, "not readable")

    # ------------------------------------------------------------------ actions

    def terminate(self, pid: int) -> tuple[bool, str]:
        """Send ``SIGTERM`` to a process, asking it to exit cleanly."""
        return self._signal(pid, "terminate")

    def kill(self, pid: int) -> tuple[bool, str]:
        """Send ``SIGKILL``, which the process cannot refuse or clean up after."""
        return self._signal(pid, "kill")

    def suspend(self, pid: int) -> tuple[bool, str]:
        """Send ``SIGSTOP``, freezing the process."""
        return self._signal(pid, "suspend")

    def resume(self, pid: int) -> tuple[bool, str]:
        """Send ``SIGCONT``, resuming a stopped process."""
        return self._signal(pid, "resume")

    def set_nice(self, pid: int, value: int) -> tuple[bool, str]:
        """Change a process's nice value.

        Lowering the nice value (raising priority) requires ``CAP_SYS_NICE``, so
        it commonly fails for an unprivileged user -- reported as a clear message
        rather than an exception.
        """
        try:
            psutil.Process(pid).nice(value)
        except psutil.NoSuchProcess:
            return False, f"Process {pid} no longer exists"
        except psutil.AccessDenied:
            return False, "Permission denied: raising priority needs root"
        except (psutil.Error, OSError, ValueError) as exc:
            return False, str(exc)
        return True, f"Nice value of PID {pid} set to {value}"

    def set_affinity(self, pid: int, cpus: list[int]) -> tuple[bool, str]:
        """Pin a process to a set of CPUs."""
        if not cpus:
            return False, "At least one CPU must be selected"
        try:
            psutil.Process(pid).cpu_affinity(cpus)
        except psutil.NoSuchProcess:
            return False, f"Process {pid} no longer exists"
        except psutil.AccessDenied:
            return False, "Permission denied: changing affinity needs privileges"
        except (psutil.Error, OSError, ValueError) as exc:
            return False, str(exc)
        return True, f"PID {pid} pinned to CPUs {', '.join(map(str, cpus))}"

    def _signal(self, pid: int, action: str) -> tuple[bool, str]:
        """Dispatch a signal-sending action with uniform error reporting."""
        if pid == os.getpid():
            return False, "Refusing to signal the monitor's own process"
        if pid == 1:
            return False, "Refusing to signal PID 1 (init)"
        try:
            proc = psutil.Process(pid)
            getattr(proc, action)()
        except psutil.NoSuchProcess:
            return False, f"Process {pid} no longer exists"
        except psutil.AccessDenied:
            return False, f"Permission denied: cannot {action} PID {pid}"
        except (psutil.Error, OSError) as exc:
            return False, str(exc)
        return True, f"Sent {action} to PID {pid}"
