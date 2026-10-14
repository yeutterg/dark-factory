"""Existing synchronous execution behind the workflow lifecycle interface."""


class NativeRuntime:
    name = "native"

    def validate(self):
        return {"engine": self.name}

    def run(self, claim, execute):
        execute()

    def pending_attempts(self):
        return []
