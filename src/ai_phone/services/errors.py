"""Recoverable brain failures. The worker reports them as `brain_error` events instead of dying."""


class BrainError(RuntimeError):
    """`kind` is what the controller logs and the wizard turns into a sentence.

    `detail` is only ever as safe as whoever built the error: a provider's own words never reach it (the CLI brains
    classify those and keep the text in the debug log), so the few kinds whose detail is a value ai-phone itself
    produced -- a login method, a configuration flag -- may show it to the person, and nothing else may.
    """

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail
