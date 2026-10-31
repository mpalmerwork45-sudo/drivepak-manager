class DrivePakError(Exception):
    """Actionable error safe to display to the operator."""


class FormatError(DrivePakError):
    pass


class SafetyError(DrivePakError):
    pass


class Cancelled(DrivePakError):
    pass
