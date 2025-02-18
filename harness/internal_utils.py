import logging
import re


class IndentFormatter(logging.Formatter):
    def __init__(self, to_wrap: logging.Formatter | None = None) -> None:
        super().__init__()
        self._wrapped = to_wrap or logging.Formatter()

    def get_prefix_length(self, record: logging.LogRecord) -> int:
        """Get the length of the prefix of the log message. Will not give wanted results if ANSI is involved."""
        formatted = self._wrapped.format(logging.LogRecord(
            name=record.name,
            level=record.levelno,
            pathname=record.pathname,
            lineno=record.lineno,
            msg="",
            args=(),
            exc_info=None,
        ))
        formatted = formatted.splitlines()[-1]  # Might as well anticipate something scuffed
        formatted = re.sub(r"\x1b\[[0-9;]*m", "", formatted)  # Simple filter for ANSI escape codes
        return len(formatted)

    def format(self, record: logging.LogRecord) -> str:
        indent = " " * self.get_prefix_length(record)
        initial, *rest = self._wrapped.format(record).splitlines(keepends=True)
        return initial + "".join(indent + line for line in rest)
