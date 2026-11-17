class DomainError(Exception):
    """A business rule said no. `code` is stable and translated by the bot; `extra` fills its placeholders."""

    def __init__(self, status: int, code: str, **extra):
        super().__init__(code)
        self.status = status
        self.code = code
        self.extra = extra


def invalid(**extra) -> DomainError:
    return DomainError(422, "invalid", **extra)
