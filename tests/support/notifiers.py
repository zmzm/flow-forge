"""Recording and deliberately failing notification adapters."""


class RecordingNotifier:
    def __init__(self) -> None:
        self.events = []

    def task_started(self, task):
        self.events.append(("task_started", task))

    def task_completed(self, task, result):
        self.events.append(("task_completed", task, result))

    def task_failed(self, task, error):
        self.events.append(("task_failed", task, error))

    def run_completed(self, summary):
        self.events.append(("run_completed", summary))


class BreakingNotifier(RecordingNotifier):
    def task_started(self, task):
        raise RuntimeError("matrix unreachable")

    def task_completed(self, task, result):
        raise RuntimeError("matrix unreachable")

    def task_failed(self, task, error):
        raise RuntimeError("matrix unreachable")

    def run_completed(self, summary):
        raise RuntimeError("matrix unreachable")
