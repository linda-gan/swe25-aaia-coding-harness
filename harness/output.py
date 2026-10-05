"""Keep tool and command output within a size limit."""


def shorten(text: str, limit: int) -> str:
    """Return text unchanged if it fits, otherwise keep its start and end.

    The end is kept because test runners print their summary last.
    The removed part is always marked, so nobody mistakes it for full output.
    """
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    removed = len(text) - head - tail
    return f"{text[:head]}\n[output shortened: {removed} characters removed]\n{text[-tail:]}"
