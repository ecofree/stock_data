"""Retired mixed trading cockpit; historical HTML remains readable.

The daily workspace consumes sealed projections. Historical calls are refused
before opening databases, inferring positions or writing a competing report.
"""


class RetiredTerminalError(RuntimeError):
    pass


def _retired():
    raise RetiredTerminalError(
        "Legacy trading terminal is retired; no database was opened and no report was written. "
        "Use Start Research.cmd or python -m trade_system.v2.research_product serve. "
        "Historical HTML remains available; production task migration requires separate approval."
    )


def build_terminal_context(*args, **kwargs):
    return _retired()


def render_terminal_html(*args, **kwargs):
    return _retired()


def write_terminal(*args, **kwargs):
    return _retired()


if __name__ == "__main__":
    _retired()
