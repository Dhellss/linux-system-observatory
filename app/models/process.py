"""Process and thread domain models."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from app.models.base import Maybe, Snapshot


class ProcessState(enum.Enum):
    """Scheduler state, normalised across psutil's platform spellings."""

    RUNNING = "Running"
    SLEEPING = "Sleeping"
    DISK_SLEEP = "Uninterruptible"
    STOPPED = "Stopped"
    ZOMBIE = "Zombie"
    IDLE = "Idle"
    TRACED = "Traced"
    DEAD = "Dead"
    UNKNOWN = "Unknown"

    @classmethod
    def from_psutil(cls, status: str) -> ProcessState:
        """Map a psutil status string onto this enum."""
        return _PSUTIL_STATES.get(status, cls.UNKNOWN)


_PSUTIL_STATES = {
    "running": ProcessState.RUNNING,
    "sleeping": ProcessState.SLEEPING,
    "disk-sleep": ProcessState.DISK_SLEEP,
    "stopped": ProcessState.STOPPED,
    "tracing-stop": ProcessState.TRACED,
    "zombie": ProcessState.ZOMBIE,
    "dead": ProcessState.DEAD,
    "idle": ProcessState.IDLE,
    "parked": ProcessState.IDLE,
    "waking": ProcessState.RUNNING,
}


@dataclass(frozen=True, slots=True)
class ThreadInfo:
    """One thread of a process."""

    tid: int
    user_time: float
    system_time: float
    name: Maybe[str] = None
    state: Maybe[str] = None

    @property
    def cpu_time(self) -> float:
        """Total CPU seconds consumed by this thread."""
        return self.user_time + self.system_time


@dataclass(frozen=True, slots=True)
class OpenFile:
    """A file descriptor held open by a process."""

    path: str
    fd: int
    mode: Maybe[str] = None


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    """A single process as displayed in the process table.

    Fields are split into *cheap* (always collected) and *expensive* (collected
    only when a process is selected for inspection).  Enumerating open files or
    environment variables for every process on every poll would cost hundreds of
    syscalls per process; the inspector fetches them on demand instead.
    """

    pid: int
    name: str
    state: ProcessState
    cpu_percent: float = 0.0
    memory_rss_bytes: int = 0
    memory_vms_bytes: int = 0
    memory_percent: float = 0.0
    ppid: Maybe[int] = None
    username: Maybe[str] = None
    num_threads: int = 1
    nice: Maybe[int] = None
    priority: Maybe[int] = None
    create_time: Maybe[float] = None
    #: Seconds of wall-clock time since the process started.
    runtime: Maybe[float] = None
    cpu_time: Maybe[float] = None
    exe: Maybe[str] = None
    cmdline: Maybe[str] = None
    cwd: Maybe[str] = None
    io_read_bytes_per_s: Maybe[float] = None
    io_write_bytes_per_s: Maybe[float] = None
    net_bytes_per_s: Maybe[float] = None
    gpu_memory_bytes: Maybe[int] = None
    cpu_affinity: tuple[int, ...] = ()
    num_fds: Maybe[int] = None
    num_ctx_switches: Maybe[int] = None
    #: Populated only by the inspector, never by the bulk collector.
    threads: tuple[ThreadInfo, ...] = ()
    open_files: tuple[OpenFile, ...] = ()
    environment: dict[str, str] = field(default_factory=dict)
    connections: int = 0
    #: True when a field could not be read because of permissions.
    restricted: bool = False

    @property
    def display_command(self) -> str:
        """Best available human-readable command for this process."""
        if isinstance(self.cmdline, str) and self.cmdline:
            return self.cmdline
        if isinstance(self.exe, str) and self.exe:
            return self.exe
        return f"[{self.name}]"

    @property
    def is_kernel_thread(self) -> bool:
        """True for kernel worker threads, which have no executable or cmdline."""
        return not self.cmdline and (self.ppid in (0, 2) or self.pid == 2)


@dataclass(frozen=True, slots=True)
class ProcessTreeNode:
    """A node in the parent/child process hierarchy."""

    process: ProcessInfo
    children: tuple[ProcessTreeNode, ...] = ()

    @property
    def subtree_cpu(self) -> float:
        """Aggregate CPU usage of this process and all descendants."""
        return self.process.cpu_percent + sum(c.subtree_cpu for c in self.children)

    @property
    def subtree_memory(self) -> int:
        """Aggregate resident memory of this process and all descendants."""
        return self.process.memory_rss_bytes + sum(
            c.subtree_memory for c in self.children
        )


@dataclass(frozen=True, slots=True)
class ProcessSnapshot(Snapshot):
    """One complete process-table sample."""

    processes: tuple[ProcessInfo, ...] = ()
    total: int = 0
    running: int = 0
    sleeping: int = 0
    stopped: int = 0
    zombie: int = 0
    threads: int = 0
    #: Processes whose details were partly unreadable without elevated privileges.
    restricted_count: int = 0

    def by_pid(self) -> dict[int, ProcessInfo]:
        """Index the sample by PID."""
        return {p.pid: p for p in self.processes}

    def top_cpu(self, limit: int = 5) -> list[ProcessInfo]:
        """The ``limit`` heaviest CPU consumers."""
        return sorted(self.processes, key=lambda p: p.cpu_percent, reverse=True)[:limit]

    def top_memory(self, limit: int = 5) -> list[ProcessInfo]:
        """The ``limit`` heaviest memory consumers."""
        return sorted(
            self.processes, key=lambda p: p.memory_rss_bytes, reverse=True
        )[:limit]

    def build_tree(self) -> tuple[ProcessTreeNode, ...]:
        """Assemble the parent/child forest.

        Orphans -- processes whose parent exited between enumeration passes --
        become roots rather than being dropped, so the tree always accounts for
        every sampled process.
        """
        by_pid = self.by_pid()
        children: dict[int, list[ProcessInfo]] = {}
        roots: list[ProcessInfo] = []
        for proc in self.processes:
            parent = proc.ppid
            if isinstance(parent, int) and parent in by_pid and parent != proc.pid:
                children.setdefault(parent, []).append(proc)
            else:
                roots.append(proc)

        def build(proc: ProcessInfo, depth: int = 0) -> ProcessTreeNode:
            # Depth guard: a corrupt PPID cycle must not cause infinite recursion.
            kids = () if depth > 64 else tuple(
                build(child, depth + 1)
                for child in sorted(children.get(proc.pid, ()), key=lambda p: p.pid)
            )
            return ProcessTreeNode(proc, kids)

        return tuple(build(root) for root in sorted(roots, key=lambda p: p.pid))
