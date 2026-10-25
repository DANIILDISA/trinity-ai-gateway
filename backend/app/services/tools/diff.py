"""Line-based diff counting for file-tool results (tr-cy0.2.3).

`line_diff(old, new)` returns the number of lines added / removed between two
file contents, for the inline +N / −M counter shown next to the filename chip.
create_file passes old="" → every line is `added`.
"""
import difflib


def line_diff(old: str, new: str) -> dict:
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    added = 0
    removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        a=old_lines, b=new_lines, autojunk=False
    ).get_opcodes():
        if tag == "replace":
            removed += i2 - i1
            added += j2 - j1
        elif tag == "delete":
            removed += i2 - i1
        elif tag == "insert":
            added += j2 - j1
    return {"added": added, "removed": removed}
