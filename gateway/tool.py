"""Gives the investigator a `run_command` tool backed by the gateway."""

from __future__ import annotations

from typing import Any, ClassVar

from agent.data import ScenarioData
from agent.tools import Toolbox
from gateway.gateway import CommandGateway

RUN_COMMAND_SPEC: dict[str, Any] = {
    "name": "run_command",
    "description": (
        "Run one shell command on the live system. Read-only commands run at once: ps, free, df, "
        "uptime, ls logs, tail -n N <file>.log, grep [-i] [-c] [-n] <text> <file>.log, and "
        "curl http://127.0.0.1:<port>/health or /metrics. A change (`svc restart <service>` or "
        "`svc rollback <service>`) is not run: it becomes a proposal for a human. No pipes, "
        "redirects or other shell syntax."
    ),
    "parameters": {
        "type": "object",
        "properties": {"command": {"type": "string", "description": "The command line"}},
        "required": ["command"],
    },
}


class GatewayToolbox(Toolbox):
    """The usual evidence tools plus `run_command`."""

    extra_specs: ClassVar[list[dict[str, Any]]] = [RUN_COMMAND_SPEC]

    def __init__(self, data: ScenarioData, gateway: CommandGateway):
        super().__init__(data)
        self.gateway = gateway

    def call(self, name: str, args: dict[str, Any]) -> str:
        if name != "run_command":
            return super().call(name, args)
        command = args.get("command")
        if not isinstance(command, str):
            return "error: run_command needs a `command` string"
        return str(self.gateway.run(command))
