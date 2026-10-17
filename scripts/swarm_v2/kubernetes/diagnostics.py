from typing import Protocol


class Logs(Protocol):
    def pod_logs(self, name: str, container: str) -> str: ...


def startup(pod: dict, api: Logs) -> dict | None:
    status = pod.get("status", {})
    containers = status.get("initContainerStatuses", []) + status.get("containerStatuses", [])
    terminated = [entry for entry in containers if "terminated" in entry.get("state", {})]
    if status.get("phase") != "Failed" and not any(
        entry["state"]["terminated"].get("exitCode", 0) != 0 for entry in terminated
    ):
        return None
    return {
        "phase": status.get("phase", ""),
        "reason": status.get("reason", ""),
        "containers": [
            {
                "name": entry["name"],
                "terminated": entry["state"]["terminated"],
                "log": api.pod_logs(pod["metadata"]["name"], entry["name"]),
            }
            for entry in terminated
        ],
    }
