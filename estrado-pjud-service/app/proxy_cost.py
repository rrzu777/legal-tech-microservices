"""Shared fail-closed errors for proxy budget and telemetry control."""


class ProxyBudgetExceededError(RuntimeError):
    """An atomic hard budget denied the operation before provider traffic."""

    def __init__(self, blocking_scope: str | None = None):
        self.blocking_scope = blocking_scope or "unknown"
        super().__init__("proxy budget denied")


class ProxyUsagePersistenceError(RuntimeError):
    """Usage could not be durably reconciled; paid traffic must fail closed."""


def is_proxy_cost_control_error(exc: BaseException) -> bool:
    return isinstance(exc, (ProxyBudgetExceededError, ProxyUsagePersistenceError))
