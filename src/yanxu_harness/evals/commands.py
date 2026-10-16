"""One bounded command runner, used for every command the evaluation executes.

Three paths run a command: the fixture's precondition check, the small `run_tests` tool the
review cases call, and the verifier's own final check. They all go through :func:`run_command`,
because the alternative — three call sites with their own `subprocess.run` — is how a limit ends
up documented in one place and enforced in none.

What went wrong before (RDE-E3 in `docs/runtime-diagnostics-evaluation-acceptance-review.zh-CN.md`
§5): the runner used ``capture_output=True``, so the child's whole output was read into memory
first and only *published* trimmed. A test run that printed 256 KiB was held in full, and the
timeout did nothing about the bytes already collected. This module bounds what is **kept while
reading**:

* both streams are read continuously by their own thread, in fixed-size chunks, keeping at most
  the policy's byte allowance and counting what it dropped. Memory does not grow with the output,
  and neither does the published evidence;
* the default strategy *drains* instead of stopping, so the child never blocks on a full pipe;
  ``stop-on-limit`` is available as an explicit strategy, and it means the process is stopped and
  reaped and the result says ``output_limit_exceeded`` rather than pretending the command failed
  or succeeded;
* the effective timeout is ``min(policy.timeout_seconds, deadline_seconds)``: a caller with a
  smaller remaining budget wins. On expiry the process is terminated, and the cleanup has its own
  bound instead of waiting forever on a pipe a grandchild keeps open;
* nothing is written to a temporary file. Spilling unbounded output to disk would dodge the limit
  rather than enforce it.

Two limits are stated rather than implied. **Descendant cleanup** is implemented per platform —
a POSIX process group, or ``taskkill /T`` on Windows — and the result says which capability it
used; only the platform this build actually ran on is described as verified. **Unproven
stopping** is reported: if a stream's reader has not reached EOF by the grace deadline (a
grandchild can hold the pipe open), the result says ``readers_finished: false`` and the caller
must treat the remaining output as unknown rather than as "nothing was printed".
"""

from __future__ import annotations

import codecs
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..errors import ContractError

__all__ = [
    "CommandPolicy",
    "CommandResult",
    "DEFAULT_COMMAND_POLICY",
    "KEEP_BOUNDED_DRAIN",
    "STOP_ON_LIMIT",
    "descendant_cleanup_capability",
    "run_command",
]

KEEP_BOUNDED_DRAIN = "keep-bounded-drain"
STOP_ON_LIMIT = "stop-on-limit"
_STRATEGIES = (KEEP_BOUNDED_DRAIN, STOP_ON_LIMIT)

#: How much of one chunk is read at a time. The reader's own buffer, so "bounded" is true of the
#: reading as well as of what is kept.
_READ_CHUNK_BYTES = 64 * 1024

#: How often the enforcement loop looks at the process and the limit flag. This is a check
#: interval, not a scheduling assumption: nothing in the evaluation depends on two events landing
#: in a particular order inside it.
_ENFORCE_INTERVAL_SECONDS = 0.01


@dataclass(frozen=True)
class CommandPolicy:
    """The limits one command is run under.

    The policy is part of the *question* a case asks (it is hashed into the case digest), so a
    change here makes earlier results incomparable rather than quietly comparable.
    """

    timeout_seconds: float = 120.0
    keep_stdout_bytes: int = 65536
    keep_stderr_bytes: int = 16384
    strategy: str = KEEP_BOUNDED_DRAIN
    #: How long a stopped process and its readers get to finish before the result says the
    #: cleanup was not proven.
    cleanup_grace_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ContractError("a command policy needs a positive timeout")
        if self.keep_stdout_bytes <= 0 or self.keep_stderr_bytes <= 0:
            raise ContractError("a command policy must keep at least one byte of each stream")
        if self.strategy not in _STRATEGIES:
            raise ContractError(
                f"unknown command output strategy {self.strategy!r}; known: {_STRATEGIES}"
            )
        if self.cleanup_grace_seconds < 0:
            raise ContractError("the cleanup grace cannot be negative")

    @property
    def digest(self) -> str:
        """The identity of these limits: what makes two policies the same policy."""
        return _policy_digest(self)

    def to_record(self) -> Mapping[str, Any]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "keep_stdout_bytes": self.keep_stdout_bytes,
            "keep_stderr_bytes": self.keep_stderr_bytes,
            "strategy": self.strategy,
            "cleanup_grace_seconds": self.cleanup_grace_seconds,
            "digest": self.digest,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "CommandPolicy":
        return cls(
            timeout_seconds=float(record.get("timeout_seconds") or 120.0),
            keep_stdout_bytes=int(record.get("keep_stdout_bytes") or 65536),
            keep_stderr_bytes=int(record.get("keep_stderr_bytes") or 16384),
            strategy=str(record.get("strategy") or KEEP_BOUNDED_DRAIN),
            cleanup_grace_seconds=float(record.get("cleanup_grace_seconds") or 5.0),
        )


def _policy_digest(policy: "CommandPolicy") -> str:
    from ..versioning import fingerprint

    return fingerprint(
        {
            "timeout_seconds": policy.timeout_seconds,
            "keep_stdout_bytes": policy.keep_stdout_bytes,
            "keep_stderr_bytes": policy.keep_stderr_bytes,
            "strategy": policy.strategy,
            "cleanup_grace_seconds": policy.cleanup_grace_seconds,
        }
    )


#: The policy every case uses unless it declares its own.
DEFAULT_COMMAND_POLICY = CommandPolicy()


@dataclass(frozen=True)
class CommandResult:
    """What one bounded command produced, and what could not be established about it."""

    argv: tuple[str, ...] = ()
    cwd: str = ""
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_bytes_kept: int = 0
    stderr_bytes_kept: int = 0
    stdout_bytes_dropped: int = 0
    stderr_bytes_dropped: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    strategy: str = KEEP_BOUNDED_DRAIN
    output_limit_exceeded: bool = False
    timed_out: bool = False
    duration_seconds: float = 0.0
    #: The capability used to stop the process and its descendants, or why none was.
    descendants_cleaned: str = ""
    #: False when a reader had not reached EOF inside the grace: some output may be unread.
    readers_finished: bool = True
    #: True when the process was proven to have exited.
    reaped: bool = True
    error: str = ""
    policy: CommandPolicy = field(default=DEFAULT_COMMAND_POLICY)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.error

    def _publish_argv(self) -> list[str]:
        """The argv as it may be published: paths replaced, nothing else changed.

        The first element is the interpreter the fixture names with a placeholder, resolved to a
        real path at execution time; publishing that resolution would put this machine's
        virtualenv into every result file.
        """
        from .verifiers import scrub_command_text

        out: list[str] = []
        for item in self.argv:
            value = scrub_command_text(item, Path(self.cwd)) if self.cwd else item
            from ..diagnostics.render import sanitise_text

            out.append(sanitise_text(value))
        return out

    def summary(self, workspace: Path | str | None = None) -> str:
        """The bounded, path-free, secret-free view of this result's output, for a report.

        Two rules apply to anything published, and a collection limit does not cancel either:
        host paths are replaced with markers (the workspace is the command's own cwd when the
        caller does not name one), and credential-shaped text is removed. The second rule lives
        with the diagnostics output boundary, which is the one place that decides what a
        published field may contain; sharing it keeps one list of shapes instead of two that
        drift apart.
        """
        from .verifiers import summarise_command_output

        text = (self.stdout or "") + (self.stderr or "")
        target = workspace if workspace is not None else (self.cwd or None)
        if target:
            text = summarise_command_output(text, Path(target))
        from ..diagnostics.render import sanitise_text

        return sanitise_text(text)

    def to_record(self, *, workspace: Path | str | None = None) -> Mapping[str, Any]:
        """The publishable form: no host path, and text that went through both scrubbers.

        ``cwd`` is published as ``<workspace>``: a result file must not carry the absolute path
        of the machine it ran on, and the same rule the reports follow applies here.
        """
        return {
            "argv": self._publish_argv(),
            "cwd": "<workspace>",
            "returncode": self.returncode,
            "output": self.summary(workspace),
            "stdout_bytes_kept": self.stdout_bytes_kept,
            "stderr_bytes_kept": self.stderr_bytes_kept,
            "stdout_bytes_dropped": self.stdout_bytes_dropped,
            "stderr_bytes_dropped": self.stderr_bytes_dropped,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "output_limit_exceeded": self.output_limit_exceeded,
            "timed_out": self.timed_out,
            "duration_seconds": self.duration_seconds,
            "descendants_cleaned": self.descendants_cleaned,
            "readers_finished": self.readers_finished,
            "reaped": self.reaped,
            "error": self.error,
            "execution": self.policy.to_record(),
        }


def descendant_cleanup_capability() -> str:
    """How this platform can stop a command *and its descendants*, named honestly.

    POSIX gets a new session and a process-group signal; Windows gets ``taskkill /T``. Anything
    else gets nothing, and says so — a limit that cannot be enforced must not be advertised.
    """
    if os.name == "posix" and hasattr(os, "killpg"):
        return "posix-process-group"
    if os.name == "nt":  # pragma: no cover - not executed on this build's platform
        return "windows-taskkill"
    return "unsupported"  # pragma: no cover - no such platform in this build's tests


class _BoundedReader(threading.Thread):
    """Reads one stream to EOF, keeping at most ``limit`` bytes and counting the rest."""

    def __init__(self, stream: Any, limit: int, *, name: str, limit_hit: threading.Event | None) -> None:
        # A daemon thread on purpose: the reader cannot outlive the process, and the alternative
        # — waiting for a stream a grandchild keeps open — is the unbounded wait this module
        # exists to remove. The result says the stream was not read to the end instead.
        super().__init__(name=f"command-{name}-reader", daemon=True)
        self._stream = stream
        self._limit = limit
        self._name = name
        self._limit_hit = limit_hit
        self.kept = bytearray()
        self.dropped = 0
        self.error = ""

    def run(self) -> None:  # pragma: no cover - exercised through run_command
        try:
            stream = self._stream
            # ``read1`` (one raw read) rather than ``read`` (fill the buffer or reach EOF): on a
            # pipe the latter blocks until 64 KiB arrive, so a child that prints one line and
            # keeps the pipe open would have its line held in the kernel instead of reported.
            read = getattr(stream, "read1", None) or stream.read
            while True:
                chunk = read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                room = self._limit - len(self.kept)
                if room > 0:
                    self.kept.extend(chunk[:room])
                    self.dropped += len(chunk) - min(room, len(chunk))
                else:
                    self.dropped += len(chunk)
                if self.dropped and self._limit_hit is not None:
                    self._limit_hit.set()
        except Exception as exc:  # noqa: BLE001 - a reader failure is a fact about the run
            self.error = f"{type(exc).__name__}: {exc}"

    def text(self) -> str:
        # Decoded once, at the end, from the bytes that were kept: an incremental decode of the
        # whole stream would put the text back in memory and undo the bound.
        return self.kept.decode("utf-8", errors="replace")


def _terminate(process: subprocess.Popen, capability: str, grace: float) -> tuple[bool, str]:
    """Stop the process and its descendants; return (proven_exited, capability_used)."""
    if process.poll() is not None:
        return True, capability
    try:
        if capability == "posix-process-group":
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        elif capability == "windows-taskkill":  # pragma: no cover - other platform
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
                timeout=grace,
            )
            return process.poll() is not None, capability
        else:  # pragma: no cover - a platform without a group kill
            process.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        # The process may have exited between the poll and the signal; the wait below decides.
        pass
    try:
        process.wait(timeout=grace)
        return True, capability
    except subprocess.TimeoutExpired:
        pass
    try:
        if capability == "posix-process-group":
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:  # pragma: no cover - a platform without a group kill
            process.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        process.wait(timeout=grace)
        return True, capability
    except subprocess.TimeoutExpired:  # pragma: no cover - a process wedged in the kernel
        return False, capability


def run_command(
    argv: Sequence[str],
    *,
    cwd: Path | str,
    policy: CommandPolicy | None = None,
    deadline_seconds: float | None = None,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    """Run one command under ``policy``, with the output bounded while it is read.

    ``deadline_seconds`` is the caller's own remaining budget; the effective timeout is the
    smaller of it and the policy's, so a narrower outer deadline is never widened by a wider
    command policy.
    """
    policy = policy or DEFAULT_COMMAND_POLICY
    if not argv:
        raise ContractError("run_command needs an argv; there is nothing to execute")
    timeout = policy.timeout_seconds
    if deadline_seconds is not None:
        timeout = min(timeout, max(0.0, float(deadline_seconds)))
    command = [str(item) for item in argv]
    capability = descendant_cleanup_capability()
    limit_hit: threading.Event | None = (
        threading.Event() if policy.strategy == STOP_ON_LIMIT else None
    )
    child_env = dict(os.environ if env is None else env)
    child_env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env,
            # A new session on POSIX is what makes the group signal reach descendants.
            start_new_session=(capability == "posix-process-group"),
        )
    except OSError as exc:
        return replace(
            CommandResult(argv=tuple(command), cwd=str(cwd), policy=policy),
            returncode=None,
            error=f"could not start the command: {type(exc).__name__}: {exc}",
            duration_seconds=round(time.monotonic() - started, 3),
            descendants_cleaned="not needed: the command never started",
        )

    readers = [
        _BoundedReader(
            process.stdout, policy.keep_stdout_bytes, name="stdout", limit_hit=limit_hit
        ),
        _BoundedReader(
            process.stderr, policy.keep_stderr_bytes, name="stderr", limit_hit=limit_hit
        ),
    ]
    for reader in readers:
        reader.start()

    timed_out = False
    output_limit_exceeded = False
    deadline = started + timeout
    while True:
        if process.poll() is not None:
            break
        if limit_hit is not None and limit_hit.is_set():
            output_limit_exceeded = True
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(min(_ENFORCE_INTERVAL_SECONDS, max(0.0, deadline - time.monotonic())))

    reaped = True
    descendants = "not needed: the command exited on its own"
    if timed_out or output_limit_exceeded:
        reaped, descendants = _terminate(process, capability, policy.cleanup_grace_seconds)

    # A reader that has not reached EOF by the grace may still be blocked on a pipe a
    # grandchild holds open. Waiting forever would be the unbounded wait this module exists to
    # remove, and closing the pipe underneath a blocked reader does not wake it on POSIX — the
    # read returns when the *last* writer closes. So the bounded wait stands and the result says
    # the rest of the stream is unknown; the reader is a daemon and cannot outlive the process.
    for reader in readers:
        reader.join(timeout=policy.cleanup_grace_seconds)
    readers_finished = all(not reader.is_alive() for reader in readers)

    stdout_bytes_kept = len(readers[0].kept)
    stderr_bytes_kept = len(readers[1].kept)
    error = ""
    if not reaped:
        error = "the process could not be proven to have exited inside the cleanup grace"
    elif not readers_finished:
        error = "a stream did not reach the end of file inside the cleanup grace"
    return CommandResult(
        argv=tuple(command),
        cwd=str(cwd),
        returncode=None if process.poll() is None else process.returncode,
        stdout=readers[0].text(),
        stderr=readers[1].text(),
        stdout_bytes_kept=stdout_bytes_kept,
        stderr_bytes_kept=stderr_bytes_kept,
        stdout_bytes_dropped=readers[0].dropped,
        stderr_bytes_dropped=readers[1].dropped,
        stdout_truncated=readers[0].dropped > 0,
        stderr_truncated=readers[1].dropped > 0,
        strategy=policy.strategy,
        output_limit_exceeded=output_limit_exceeded,
        timed_out=timed_out,
        duration_seconds=round(time.monotonic() - started, 3),
        descendants_cleaned=descendants,
        readers_finished=readers_finished,
        reaped=reaped,
        error=error,
        policy=policy,
    )


_ = (sys,)
