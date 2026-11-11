"""Executor protocol and Agy implementation for Ultron."""

from __future__ import annotations

import json
import pathlib
import subprocess
from dataclasses import dataclass
from typing import Protocol

from ultron.policy import AGY_TIMEOUT


@dataclass(frozen=True)
class Envelope:
    status: str | None
    conversation_id: str | None
    structured_output: dict | None
    denied_actions: tuple[str, ...]
    usage: dict
    num_turns: int | None
    duration_seconds: float
    raw: dict
    noise: tuple[str, ...]

    @classmethod
    def from_output(cls, text: str) -> "Envelope":
        lines = text.splitlines()
        envelope_data = None
        noise_lines = []

        for i in range(len(lines) - 1, -1, -1):
            line = lines[i]
            if not envelope_data and line.startswith("{"):
                try:
                    data = json.loads(line)
                    if isinstance(data, dict) and ("conversation_id" in data or "status" in data):
                        envelope_data = data
                        continue
                except json.JSONDecodeError:
                    pass
            if line.strip():
                noise_lines.insert(0, line)

        if not envelope_data:
            return cls(
                status="NO_OUTPUT",
                conversation_id=None,
                structured_output=None,
                denied_actions=(),
                usage={},
                num_turns=None,
                duration_seconds=0.0,
                raw={},
                noise=tuple(noise_lines)
            )

        status = envelope_data.get("status")
        conversation_id = envelope_data.get("conversation_id")
        usage = envelope_data.get("usage", {})
        num_turns = envelope_data.get("num_turns")
        duration_seconds = float(envelope_data.get("duration_seconds", 0.0))

        structured_output = envelope_data.get("structured_output")
        if structured_output is None and "response" in envelope_data:
            response = envelope_data["response"]
            if isinstance(response, str):
                try:
                    parsed_response = json.loads(response)
                    if isinstance(parsed_response, dict):
                        structured_output = parsed_response
                except json.JSONDecodeError:
                    pass

        denied_actions_list = envelope_data.get("denied_actions", [])
        denied_actions = tuple(
            action.get("display_name", "")
            for action in denied_actions_list
            if isinstance(action, dict) and "display_name" in action
        )

        return cls(
            status=status,
            conversation_id=conversation_id,
            structured_output=structured_output,
            denied_actions=denied_actions,
            usage=usage,
            num_turns=num_turns,
            duration_seconds=duration_seconds,
            raw=envelope_data,
            noise=tuple(noise_lines)
        )

    @property
    def ok(self) -> bool:
        return (
            self.status == "SUCCESS"
            and not self.denied_actions
            and self.structured_output is not None
        )


class Executor(Protocol):
    def run(
        self,
        prompt: str,
        *,
        dir: pathlib.Path,
        model: str,
        schema: pathlib.Path | None,
        conversation: str | None = None,
        mode: str | None = None,
        effort: str | None = None,
        timeout: str = AGY_TIMEOUT
    ) -> Envelope:
        ...


class AgyExecutor:
    def __init__(self, binary: str = "agy", grace_seconds: int = 60):
        self.binary = binary
        self.grace_seconds = grace_seconds

    def command(
        self,
        prompt: str,
        *,
        dir: pathlib.Path,
        model: str,
        schema: pathlib.Path | None,
        conversation: str | None = None,
        mode: str | None = None,
        effort: str | None = None,
        timeout: str = AGY_TIMEOUT
    ) -> list[str]:
        cmd = [
            self.binary,
            "--dangerously-skip-permissions",
            "-p", prompt,
            "--add-dir", str(dir),
            "--model", model,
            "--output-format", "json",
        ]
        if schema is not None:
            cmd.extend(["--json-schema", str(schema)])
        cmd.extend(["--print-timeout", timeout])
        if conversation is not None:
            cmd.extend(["--conversation", conversation])
        if mode is not None:
            cmd.extend(["--mode", mode])
        if effort is not None:
            cmd.extend(["--effort", effort])
        return cmd

    def run(
        self,
        prompt: str,
        *,
        dir: pathlib.Path,
        model: str,
        schema: pathlib.Path | None,
        conversation: str | None = None,
        mode: str | None = None,
        effort: str | None = None,
        timeout: str = AGY_TIMEOUT
    ) -> Envelope:
        cmd = self.command(
            prompt,
            dir=dir,
            model=model,
            schema=schema,
            conversation=conversation,
            mode=mode,
            effort=effort,
            timeout=timeout
        )
        timeout_seconds = parse_timeout(timeout) + self.grace_seconds

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(dir),
                capture_output=True,
                text=True,
                timeout=timeout_seconds
            )
            
            env = Envelope.from_output(proc.stdout)
            
            noise_list = list(env.noise)
            if proc.stderr:
                for line in proc.stderr.splitlines():
                    if line.strip():
                        noise_list.append(line)
            
            return Envelope(
                status=env.status,
                conversation_id=env.conversation_id,
                structured_output=env.structured_output,
                denied_actions=env.denied_actions,
                usage=env.usage,
                num_turns=env.num_turns,
                duration_seconds=env.duration_seconds,
                raw=env.raw,
                noise=tuple(noise_list)
            )
            
        except subprocess.TimeoutExpired as e:
            return Envelope(
                status="TIMEOUT",
                conversation_id=None,
                structured_output=None,
                denied_actions=(),
                usage={},
                num_turns=None,
                duration_seconds=0.0,
                raw={},
                noise=()
            )
        except FileNotFoundError as e:
            return Envelope(
                status="NO_OUTPUT",
                conversation_id=None,
                structured_output=None,
                denied_actions=(),
                usage={},
                num_turns=None,
                duration_seconds=0.0,
                raw={},
                noise=(str(e),)
            )


def parse_timeout(text: str) -> int:
    text = text.strip()
    if not text:
        raise ValueError("пустой таймаут")
    
    unit = text[-1]
    if unit == 's':
        return int(text[:-1])
    elif unit == 'm':
        return int(text[:-1]) * 60
    elif unit == 'h':
        return int(text[:-1]) * 3600
    elif text.isdigit():
        return int(text)
    else:
        raise ValueError(f"неизвестный формат таймаута: {text}")
