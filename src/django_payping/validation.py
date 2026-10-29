import re


def integer_value(value: object) -> int | None:
    """Parse an integer without truncating floats or accepting booleans."""
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]+", value):
        try:
            return int(value)
        except ValueError:
            pass
    return None
