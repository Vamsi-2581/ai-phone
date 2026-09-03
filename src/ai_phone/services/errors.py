"""Recoverable brain failures. The worker reports them as `brain_error` events instead of dying."""


class BrainError(RuntimeError):
    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
