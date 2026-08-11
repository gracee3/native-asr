#!/usr/bin/env python3
"""Isolated PipeWire loopback lifecycle for live-capture acceptance."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import tempfile
import time
from typing import Any


NODE_TYPE = "PipeWire:Interface:Node"
LINK_TYPE = "PipeWire:Interface:Link"


class PipeWireError(RuntimeError):
    """A virtual graph requirement was not met."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def node_names(run_id: str | None = None) -> tuple[str, str, str]:
    identity = run_id or f"{os.getpid()}_{secrets.token_hex(5)}"
    prefix = f"native_asr_loopback_{identity}"
    return prefix, f"{prefix}_sink", f"{prefix}_source"


def node_identity(node: dict[str, Any]) -> dict[str, Any]:
    props = node.get("info", {}).get("props", {})
    return {
        "id": node.get("id"),
        "serial": props.get("object.serial"),
        "name": props.get("node.name"),
        "description": props.get("node.description"),
        "media_class": props.get("media.class"),
        "client_id": props.get("client.id"),
        "object_path": props.get("object.path"),
        "device_id": props.get("device.id"),
        "autoconnect": props.get("node.autoconnect"),
    }


def inspect_graph(
    graph: list[dict[str, Any]], sink_name: str, source_name: str, phase: str
) -> dict[str, Any]:
    nodes = {
        int(item["id"]): item
        for item in graph
        if item.get("type") == NODE_TYPE and item.get("id") is not None
    }
    by_name: dict[str, list[dict[str, Any]]] = {}
    for node in nodes.values():
        name = node.get("info", {}).get("props", {}).get("node.name")
        if isinstance(name, str):
            by_name.setdefault(name, []).append(node)

    sink_matches = by_name.get(sink_name, [])
    source_matches = by_name.get(source_name, [])
    if len(sink_matches) != 1 or len(source_matches) != 1:
        raise PipeWireError(
            f"virtual nodes are not uniquely present: sink={len(sink_matches)}, "
            f"source={len(source_matches)}"
        )
    sink = sink_matches[0]
    source = source_matches[0]
    sink_info = node_identity(sink)
    source_info = node_identity(source)
    if sink_info["media_class"] != "Audio/Sink":
        raise PipeWireError(f"virtual sink has unexpected class: {sink_info['media_class']}")
    if source_info["media_class"] != "Audio/Source":
        raise PipeWireError(f"virtual source has unexpected class: {source_info['media_class']}")
    if sink_info["device_id"] is not None or source_info["device_id"] is not None:
        raise PipeWireError("virtual nodes unexpectedly belong to a physical device")
    if sink_info["autoconnect"] not in (False, "false"):
        raise PipeWireError("virtual sink autoconnect is not disabled")
    if source_info["autoconnect"] not in (False, "false"):
        raise PipeWireError("virtual source autoconnect is not disabled")

    sink_id = int(sink["id"])
    source_id = int(source["id"])
    virtual_ids = {sink_id, source_id}
    connections: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    capture_connected = False
    playback_connected = False
    for item in graph:
        if item.get("type") != LINK_TYPE:
            continue
        info = item.get("info", {})
        output_id = info.get("output-node-id")
        input_id = info.get("input-node-id")
        if output_id not in virtual_ids and input_id not in virtual_ids:
            continue
        output_node = nodes.get(int(output_id)) if output_id is not None else None
        input_node = nodes.get(int(input_id)) if input_id is not None else None
        output_identity = node_identity(output_node) if output_node else None
        input_identity = node_identity(input_node) if input_node else None
        connection = {
            "link_id": item.get("id"),
            "state": info.get("state"),
            "output": output_identity,
            "input": input_identity,
        }
        connections.append(connection)

        if output_id == source_id and input_identity:
            capture_connected |= input_identity["media_class"] == "Stream/Input/Audio"
        if input_id == sink_id and output_identity:
            playback_connected |= output_identity["media_class"] == "Stream/Output/Audio"

        for endpoint_id, endpoint in ((output_id, output_identity), (input_id, input_identity)):
            if endpoint_id in virtual_ids:
                continue
            allowed = endpoint and endpoint["media_class"] in {
                "Stream/Input/Audio",
                "Stream/Output/Audio",
            } and endpoint["device_id"] is None
            if not allowed:
                violations.append(connection)
                break

    return {
        "phase": phase,
        "checked_at": utc_now(),
        "passed": not violations,
        "sink": sink_info,
        "source": source_info,
        "capture_connected": capture_connected,
        "playback_connected": playback_connected,
        "connections": connections,
        "physical_or_unknown_links": violations,
    }


def terminate_owned_process(process: subprocess.Popen[Any] | None) -> int | None:
    if process is None:
        return None
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)
    return process.returncode


class PipeWireLoopback:
    def __init__(
        self,
        *,
        run_id: str | None = None,
        pw_loopback_bin: str = "pw-loopback",
        pw_play_bin: str = "pw-play",
        pw_dump_bin: str = "pw-dump",
        node_timeout: float = 5.0,
    ) -> None:
        self.prefix, self.sink_name, self.source_name = node_names(run_id)
        self.pw_loopback_bin = pw_loopback_bin
        self.pw_play_bin = pw_play_bin
        self.pw_dump_bin = pw_dump_bin
        self.node_timeout = node_timeout
        self.process: subprocess.Popen[Any] | None = None
        self.playback: subprocess.Popen[str] | None = None
        self.loopback_stderr = tempfile.TemporaryFile("w+", encoding="utf-8")
        self.graph_checks: list[dict[str, Any]] = []
        self.identities: dict[str, Any] = {}
        self.playback_status: dict[str, Any] = {"status": "not-started", "returncode": None}

    def command(self) -> list[str]:
        capture_props = {
            "node.name": self.sink_name,
            "node.description": self.sink_name,
            "media.class": "Audio/Sink",
            "audio.position": ["MONO"],
            "node.autoconnect": False,
        }
        playback_props = {
            "node.name": self.source_name,
            "node.description": self.source_name,
            "media.class": "Audio/Source",
            "audio.position": ["MONO"],
            "node.autoconnect": False,
        }
        return [
            self.pw_loopback_bin,
            "-n", self.prefix,
            "-g", self.prefix,
            "-c", "1",
            "-m", "[ MONO ]",
            "--capture-props", json.dumps(capture_props, separators=(",", ":")),
            "--playback-props", json.dumps(playback_props, separators=(",", ":")),
        ]

    def dump_graph(self) -> list[dict[str, Any]]:
        try:
            output = subprocess.check_output(
                [self.pw_dump_bin], text=True, stderr=subprocess.PIPE, timeout=5
            )
            value = json.loads(output)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
            raise PipeWireError(f"cannot inspect PipeWire graph: {error}") from error
        if not isinstance(value, list):
            raise PipeWireError("PipeWire graph dump is not a JSON array")
        return value

    def version(self) -> str:
        try:
            output = subprocess.check_output(
                [self.pw_loopback_bin, "--version"], text=True,
                stderr=subprocess.STDOUT, timeout=5,
            )
            return " ".join(line.strip() for line in output.splitlines() if line.strip())
        except (OSError, subprocess.SubprocessError):
            return "unknown"

    def start(self) -> dict[str, Any]:
        self.process = subprocess.Popen(
            self.command(), stdout=subprocess.DEVNULL, stderr=self.loopback_stderr,
            start_new_session=True,
        )
        deadline = time.monotonic() + self.node_timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.loopback_stderr.seek(0)
                detail = self.loopback_stderr.read()[-2000:]
                raise PipeWireError(
                    f"pw-loopback exited with status {self.process.returncode}: {detail}"
                )
            try:
                check = inspect_graph(
                    self.dump_graph(), self.sink_name, self.source_name, "created"
                )
                if not check["passed"]:
                    raise PipeWireError("physical node linked during virtual-node creation")
                self.graph_checks.append(check)
                self.identities = {"sink": check["sink"], "source": check["source"]}
                return self.identities
            except PipeWireError as error:
                last_error = error
                time.sleep(0.05)
        raise PipeWireError(f"virtual source or sink did not appear: {last_error}")

    def check_graph(
        self, phase: str, *, require_capture: bool = False, require_playback: bool = False,
        timeout: float = 0.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            check = inspect_graph(self.dump_graph(), self.sink_name, self.source_name, phase)
            ready = check["passed"]
            ready &= not require_capture or check["capture_connected"]
            ready &= not require_playback or check["playback_connected"]
            if not check["passed"]:
                self.graph_checks.append(check)
                raise PipeWireError("virtual graph is linked to a physical or unknown node")
            if ready:
                self.graph_checks.append(check)
                return check
            if time.monotonic() >= deadline:
                self.graph_checks.append(check)
                missing = []
                if require_capture and not check["capture_connected"]:
                    missing.append("capture")
                if require_playback and not check["playback_connected"]:
                    missing.append("playback")
                raise PipeWireError(f"expected exact virtual {'/'.join(missing)} link is absent")
            time.sleep(0.05)

    def start_playback(self, audio: Path) -> list[str]:
        command = [
            self.pw_play_bin,
            "--target", self.sink_name,
            "--latency", "50ms",
            str(audio),
        ]
        self.playback_status = {
            "status": "running",
            "started_at": utc_now(),
            "command": command,
            "target": self.sink_name,
            "returncode": None,
        }
        self.playback = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
        return command

    def wait_playback(self, timeout: float) -> dict[str, Any]:
        if self.playback is None:
            raise PipeWireError("playback was not started")
        try:
            _, stderr = self.playback.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            terminate_owned_process(self.playback)
            self.playback_status.update({
                "status": "timed-out", "returncode": self.playback.returncode,
            })
            raise PipeWireError("pw-play did not complete within its fixture bound") from error
        self.playback_status.update({
            "status": "complete" if self.playback.returncode == 0 else "failed",
            "completed_at": utc_now(),
            "returncode": self.playback.returncode,
            "stderr_tail": stderr[-2000:],
        })
        if self.playback.returncode:
            raise PipeWireError(f"pw-play failed with status {self.playback.returncode}")
        return self.playback_status

    def close(self) -> dict[str, Any]:
        playback_returncode = terminate_owned_process(self.playback)
        loopback_returncode = terminate_owned_process(self.process)
        deadline = time.monotonic() + self.node_timeout
        nodes_absent = False
        cleanup_error = None
        while time.monotonic() < deadline:
            try:
                graph = self.dump_graph()
                names = {
                    item.get("info", {}).get("props", {}).get("node.name")
                    for item in graph if item.get("type") == NODE_TYPE
                }
                if self.sink_name not in names and self.source_name not in names:
                    nodes_absent = True
                    break
            except PipeWireError as error:
                cleanup_error = str(error)
                break
            time.sleep(0.05)
        self.loopback_stderr.seek(0)
        stderr_tail = self.loopback_stderr.read()[-2000:]
        return {
            "status": "complete" if nodes_absent else "failed",
            "checked_at": utc_now(),
            "playback_returncode": playback_returncode,
            "loopback_returncode": loopback_returncode,
            "virtual_nodes_absent": nodes_absent,
            "stderr_tail": stderr_tail,
            "error": cleanup_error,
        }
