"""The use cases, as seen by the controllers. The composition root (main.py) installs them at startup.

Controllers write `S.money.unpaid(team_id)`; they never build or reach past the use cases."""


class _Services:
    _target = None

    def __getattr__(self, name):
        if self._target is None:
            raise RuntimeError("use cases are not installed; main.py does this at startup")
        return getattr(self._target, name)


S = _Services()


def install(services) -> None:
    S._target = services
