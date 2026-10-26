"""Typed errors of the driver. Each says what happened; none suggests a way around a check."""

from __future__ import annotations


class DriverError(Exception):
    """Base of every error the driver raises on purpose."""


class ConfigError(DriverError):
    """A required setting (environment variable or path) is missing or wrong."""


class TaskInvalid(DriverError):
    """`task.json` does not have the shape CONTRACTS section 9 requires."""


class SwitchInvalid(TaskInvalid):
    """A switch is unknown or has a value section 7 does not list."""


class PluginInvalid(DriverError):
    """The plugin source cannot be assembled into a run plugin."""


class FragmentMissing(PluginInvalid):
    """A switch value has no fragment file (an empty file is required where it adds nothing)."""


class AgentFileInvalid(PluginInvalid):
    """An agent file's frontmatter is missing, unparseable, or lacks a required field."""


class HarnessError(DriverError):
    """The pinned harness binary did not behave as expected (version, start, stream)."""


class RunStateError(DriverError):
    """The run folder or the driver's state file is not in a state this operation needs."""


class DriverBusy(RunStateError):
    """Another driver process holds this run folder's lock."""
