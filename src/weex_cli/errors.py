from __future__ import annotations


class WeexCliError(Exception):
    """Base error for expected CLI failures."""


class ConfigurationError(WeexCliError):
    """Configuration is missing or invalid."""


class SafetyError(WeexCliError):
    """An execution safety condition was not satisfied."""


class ValidationError(WeexCliError):
    """An order or command argument is invalid."""


class UnsupportedModeError(WeexCliError):
    """The requested operation is unavailable in the selected mode."""


class SubmissionUncertainError(WeexCliError):
    """An order may have reached WEEX even though submission raised an error."""
