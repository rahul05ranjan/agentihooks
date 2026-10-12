"""A scripted Codex app-server: the methods the supervisor calls, with one attribute per fault."""

from pathlib import Path

from scripts.codex_session import supervisor

OWN_START = 11
SERVER_START = 77
FIRST_PID = 4_190_001


def hooks(source, trust="trusted", events=supervisor.REQUIRED_HOOKS):
    return [
        {"eventName": event, "trustStatus": trust, "key": f"k:{event}", "currentHash": "h", "sourcePath": source}
        for event in events
    ]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Process:
    def __init__(self, pid):
        self.pid = pid
        self.returncode = None
        self.stopped = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.stopped = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class Connection:
    def __init__(self, server):
        self.server = server
        self.closed = False

    def request(self, method, params=None):
        self.server.requests.append((method, params))
        if method == "hooks/list":
            return {"data": [{"hooks": self.server.hooks}]}
        if method == "config/batchWrite":
            self.server.hooks = [{**hook, "trustStatus": self.server.trust_to} for hook in self.server.hooks]
            return {}
        if method == "account/read":
            return {"account": {"type": self.server.account}}
        if method == "thread/start":
            return {"thread": {"id": "thread-new", "sessionId": "session-new"}}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"], "sessionId": "session-resumed"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-opening"}}
        raise AssertionError(f"unexpected method {method}")

    def close(self):
        self.closed = True


class Server:
    def __init__(self, codex_home):
        self.codex_home = codex_home
        self.reported_home = codex_home
        self.source = f"{codex_home}/hooks.json"
        self.hooks = hooks(self.source)
        self.trust_to = "trusted"
        self.account = "chatgpt"
        self.answers = True
        self.exits = False
        self.requests = []
        self.processes = []
        self.commands = []
        self.envs = []
        self.starts = {}
        self.connections = []

    def popen(self, command, env, cwd, stdin, start_new_session):
        self.commands.append(command)
        self.envs.append(env)
        Path(command[command.index("--listen") + 1].removeprefix("unix://")).touch()
        process = Process(FIRST_PID + len(self.processes))
        process.returncode = 1 if self.exits else None
        self.processes.append(process)
        self.starts[process.pid] = SERVER_START
        return process

    def start_identity(self, pid):
        return self.starts.get(pid, OWN_START)

    def connect(self, path, timeout):
        if not self.answers:
            raise TimeoutError("no reply to initialize")
        connection = Connection(self)
        self.connections.append(connection)
        return connection, {"codexHome": self.reported_home}

    def methods(self):
        return [method for method, _ in self.requests]
