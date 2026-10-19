"""Exception types shared by the engine, the CLI and the GUI."""

from __future__ import annotations


class ImportToolError(Exception):
    """Base class for errors this tool raises on purpose."""


class ConfigError(ImportToolError):
    """The user's config.toml is invalid. Import must stay disabled until it is fixed."""

    def __init__(self, msg: str, line: int | None = None, col: int | None = None):
        self.msg, self.line, self.col = msg, line, col
        where = f" (line {line}, column {col})" if line is not None else ""
        super().__init__(f"{msg}{where}")


class PlanBlocked(ImportToolError):
    """The plan has blocking problems; nothing may be copied."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems) or "plan blocked")


class AnotherImportRunning(ImportToolError):
    """Another import or delete holds the import lock."""

    def __init__(self, msg: str = "Another import is already running."):
        super().__init__(msg)


class SourceChanged(ImportToolError):
    """A card file changed (size or mtime) since it was scanned."""


class SourceGone(ImportToolError):
    """A card (or source folder) disappeared during the import."""


class VerifyMismatch(ImportToolError):
    """The copy's checksum does not match the bytes read from the card."""


class HardwareSuspect(ImportToolError):
    """Too many checksum mismatches in one session: the card reader or cable is suspect."""


class DestFull(ImportToolError):
    """The destination drive ran out of space."""


class Cancelled(Exception):
    """The user cancelled the running job. Not an ImportToolError: it is not a failure."""
