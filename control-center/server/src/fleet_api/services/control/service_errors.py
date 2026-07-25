class FleetError(RuntimeError):
    status_code = 400


class InstanceNotFound(FleetError):
    status_code = 404


class StrategyNotFound(FleetError):
    status_code = 404


class UnsafeOperation(FleetError):
    status_code = 409


class ValidationFailed(FleetError):
    status_code = 422


class TelemetryUnavailable(FleetError):
    status_code = 503


class BetaSourceUnavailable(TelemetryUnavailable):
    """The Final Beta source cannot safely produce a new Live preview."""
