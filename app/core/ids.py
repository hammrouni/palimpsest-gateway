"""UUID generation utilities."""
from fastuuid import uuid7


def new_id() -> str:
    """Generate a new UUIDv7 as a string.

    Returns:
        UUIDv7 string (time-sortable)
    """
    return str(uuid7())
