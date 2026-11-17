class AppError(Exception):
    """Raised by services and routes; rendered as {"detail": {"code": ..., **extra}}."""

    def __init__(self, status: int, code: str, **extra):
        super().__init__(code)
        self.status = status
        self.code = code
        self.extra = extra
