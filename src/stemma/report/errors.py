"""The one error a reader of a run raises: it names what it could not read and why."""


class RunReadError(Exception):
    """A run folder lacks something every run has, holds it in an unreadable or unsafe
    form, or is larger than a resource limit."""
