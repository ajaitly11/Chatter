"""Small, testable text-editing helpers for the Live Dictation editor."""


def current_line_bounds(block_position: int, block_length: int) -> tuple[int, int]:
    """Return a QTextCursor block's content bounds.

    QTextCursor positions are UTF-16 offsets, so using the native block
    position and length avoids mismatches around emoji and other non-BMP
    characters. ``QTextBlock.length()`` includes its paragraph separator;
    exclude that separator so clearing a line does not join adjacent lines.
    """
    start = max(0, block_position)
    end = start + max(0, block_length - 1)
    return start, max(start, end)
