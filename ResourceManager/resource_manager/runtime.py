"""Constrained Docker process execution with opaque, bounded line transport."""

import json
import os
import re
import selectors
import subprocess
import time
from decimal import Decimal

from .models import MAX_LINE_BYTES
from .queue import JobCancelled, OutputLimit

MAX_STREAM_BYTES = 16 * 1024 * 1024


class RuntimeFailure(Exception):
    """A safe error code; raw Docker diagnostics are never sent to API callers."""


class DockerRuntime:
    def __init__(self, resource, docker_command=None):
        self.resource = resource
        self.command = tuple(
            docker_command
            or (
                ("docker", "--context", resource.docker_context)
                if resource.docker_context
                else ("docker",)
            )
        )
        self.cleanup_confirmed = True
        self.image_id = None

    def command_output(self, args, timeout):
        """Control-plane output is bounded independently of the attached process."""
        process = None
        try:
            process = subprocess.Popen(
                [*self.command, *args],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            chunks = {process.stdout: bytearray(), process.stderr: bytearray()}
            deadline = time.monotonic() + timeout
            with selectors.DefaultSelector() as selector:
                for stream in chunks:
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeFailure("docker_control_timeout")
                    for key, _ in selector.select(min(remaining, 0.1)):
                        try:
                            chunk = os.read(key.fileobj.fileno(), 65536)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        chunks[key.fileobj].extend(chunk)
                        if sum(map(len, chunks.values())) > 1024 * 1024:
                            raise RuntimeFailure("docker_control_output_limit")
                try:
                    process.wait(timeout=max(0.001, deadline - time.monotonic()))
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeFailure("docker_control_timeout") from exc
            return process.returncode, bytes(chunks[process.stdout]), bytes(chunks[process.stderr])
        except OSError as exc:
            raise RuntimeFailure("docker_unavailable") from exc
        finally:
            if process is not None:
                self.stop_client(process)

    @staticmethod
    def stop_client(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    def check_ready(self, timeout=10):
        started = time.monotonic()
        status, _, _ = self.command_output(["info", "--format", "{{json .ServerVersion}}"], timeout)
        if status:
            raise RuntimeFailure("docker_daemon_unavailable")
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise RuntimeFailure("docker_control_timeout")
        status, output, _ = self.command_output(
            ["image", "inspect", self.resource.image], remaining
        )
        if status:
            raise RuntimeFailure("configured_image_not_installed")
        try:
            images = json.loads(output)
            if not isinstance(images, list) or len(images) != 1:
                raise ValueError
            image = images[0]
            image_id = image["Id"]
            if re.fullmatch(r"sha256:[a-f0-9]{64}", image_id) is None:
                raise ValueError
            if not isinstance(image["Config"], dict):
                raise ValueError
            if image["Config"].get("Volumes"):
                raise RuntimeFailure("image_declares_disallowed_volumes")
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeFailure("invalid_image_metadata") from exc
        self.image_id = image_id

    def create_args(self, job):
        # No submitted value controls an image, command, mount, environment, or network.
        return [
            "create",
            "--interactive",
            "--pull=never",
            "--name",
            job["sandbox_id"],
            "--network=none",
            "--read-only",
            "--user=65532:65532",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=128",
            f"--memory={job['memory_mb']}m",
            f"--memory-swap={job['memory_mb']}m",
            f"--cpus={format(Decimal(str(job['cpus'])), 'f')}",
            f"--tmpfs=/tmp:rw,noexec,nosuid,nodev,size={job['scratch_mb']}m,mode=1777",
            "--log-driver=none",
            "--no-healthcheck",
            self.image_id,
        ]

    def cleanup(self, name):
        try:
            status, _, error = self.command_output(["rm", "--force", name], 10)
            # Docker is authoritative that the UUID container is already absent.
            return status == 0 or b"no such container" in error.lower()
        except RuntimeFailure:
            return False

    def run(self, job, token, queue):
        deadline = time.monotonic() + job["timeout_seconds"]
        process = None
        create_attempted = False
        create_acknowledged = False
        create_uncertain = False
        self.cleanup_confirmed = True
        outcome = None
        try:
            if not queue.heartbeat(job["id"], token):
                raise JobCancelled
            if self.image_id is None:
                self.check_ready(min(10, max(0.001, deadline - time.monotonic())))
            if not queue.heartbeat(job["id"], token):
                raise JobCancelled
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeFailure("compute_timeout")
            create_attempted = True
            self.cleanup_confirmed = False
            try:
                status, output, _ = self.command_output(self.create_args(job), min(10, remaining))
            except RuntimeFailure:
                # A daemon may finish a timed-out create later. Never start it, and
                # retain the reservation even if an immediate remove finds no object.
                create_uncertain = True
                raise
            if status or re.fullmatch(rb"[a-f0-9]{12,64}\s*", output) is None:
                create_uncertain = status == 0
                raise RuntimeFailure("container_create_failed")
            create_acknowledged = True
            if not queue.heartbeat(job["id"], token):
                raise JobCancelled
            if time.monotonic() >= deadline:
                raise RuntimeFailure("compute_timeout")
            process = subprocess.Popen(
                [*self.command, "start", "--attach", "--interactive", job["sandbox_id"]],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            queue.mark_running(job["id"], token)
            self.relay(process, job, token, queue, deadline)
            # A successful client disconnect alone is insufficient to establish exit.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeFailure("compute_timeout")
            status, output, _ = self.command_output(
                ["inspect", "--format", "{{json .State}}", job["sandbox_id"]], min(10, remaining)
            )
            try:
                state = json.loads(output)
                if (
                    status
                    or state["Running"] is not False
                    or isinstance(state["ExitCode"], bool)
                    or not isinstance(state["ExitCode"], int)
                ):
                    raise ValueError
                outcome = state["ExitCode"]
            except (ValueError, TypeError, KeyError) as exc:
                raise RuntimeFailure("container_exit_unconfirmed") from exc
        except OSError as exc:
            raise RuntimeFailure("docker_unavailable") from exc
        finally:
            if create_attempted:
                removed = self.cleanup(job["sandbox_id"])
                self.cleanup_confirmed = removed and not create_uncertain
            if process is not None:
                self.stop_client(process)
            if create_attempted and not create_acknowledged and create_uncertain:
                self.cleanup_confirmed = False
        if not self.cleanup_confirmed:
            raise RuntimeFailure("container_cleanup_unconfirmed")
        return outcome

    def relay(self, process, job, token, queue, deadline):
        """Relay bytes without interpreting caller or process payloads."""
        stdout_buffer = bytearray()
        stdout_size = 0
        stderr_size = 0
        outgoing = None
        outgoing_sequence = None
        last_heartbeat = 0.0
        stdin_closed = False

        def check_active():
            nonlocal last_heartbeat
            now = time.monotonic()
            if now >= deadline:
                raise RuntimeFailure("compute_timeout")
            if now - last_heartbeat >= 1:
                if not queue.heartbeat(job["id"], token):
                    raise JobCancelled
                last_heartbeat = now

        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            os.set_blocking(process.stdin.fileno(), False)
            while True:
                check_active()
                if not stdin_closed and outgoing is None:
                    message, closed = queue.pending_stdin(job["id"])
                    if message is not None:
                        outgoing = memoryview((message["line"] + "\n").encode("utf-8"))
                        outgoing_sequence = message["sequence"]
                        selector.register(process.stdin, selectors.EVENT_WRITE)
                    elif closed:
                        process.stdin.close()
                        stdin_closed = True
                for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                    stream = key.fileobj
                    if stream is process.stdin:
                        try:
                            written = os.write(stream.fileno(), outgoing[:65536])
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            selector.unregister(stream)
                            stream.close()
                            stdin_closed = True
                            outgoing = None
                            continue
                        outgoing = outgoing[written:]
                        if not outgoing:
                            queue.ack_stdin(job["id"], outgoing_sequence)
                            outgoing = None
                            selector.unregister(stream)
                        continue
                    try:
                        chunk = os.read(stream.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(stream)
                        continue
                    if stream is process.stderr:
                        stderr_size += len(chunk)
                        if stderr_size > MAX_STREAM_BYTES:
                            raise RuntimeFailure("stderr_output_limit")
                        continue
                    stdout_size += len(chunk)
                    if stdout_size > MAX_STREAM_BYTES:
                        raise OutputLimit
                    stdout_buffer.extend(chunk)
                    lines = stdout_buffer.split(b"\n")
                    stdout_buffer = lines.pop()
                    for line in lines:
                        # A flood of short lines must not postpone leases or deadlines.
                        check_active()
                        self.record_line(queue, job, token, line)
                    if len(stdout_buffer) > MAX_LINE_BYTES:
                        raise OutputLimit
                reading = any(
                    key.fileobj is not process.stdin for key in selector.get_map().values()
                )
                if process.poll() is not None and not reading:
                    if stdout_buffer:
                        self.record_line(queue, job, token, stdout_buffer)
                    return

    @staticmethod
    def record_line(queue, job, token, line):
        if len(line) > MAX_LINE_BYTES:
            raise OutputLimit
        try:
            decoded = bytes(line).decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise RuntimeFailure("stdout_not_utf8") from exc
        queue.append_stdout(job["id"], token, decoded)
