"""Dependency-free exception types shared by media planning and execution."""


class MediaOperationError(RuntimeError):
    """An anticipated media, input, dependency or external-tool failure.

    Log its message and operation context without a Python traceback. Programming
    errors and broken internal invariants must keep their original exception type.
    """
