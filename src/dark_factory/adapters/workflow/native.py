"""Existing synchronous execution behind the workflow lifecycle interface."""


class NativeRuntime:
    name = "native"

    def run(self, claim, execute):
        execute()

    def pending_attempts(self):
        return []
