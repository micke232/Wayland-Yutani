"""Copy an explicit user selection to the local system clipboard."""

import shutil
import subprocess


def copy_text(text):
    if shutil.which("pbcopy"):
        command = ["pbcopy"]
    elif shutil.which("wl-copy"):
        command = ["wl-copy"]
    elif shutil.which("xclip"):
        command = ["xclip", "-selection", "clipboard"]
    else:
        raise RuntimeError("No clipboard helper found (pbcopy, wl-copy, or xclip)")
    subprocess.run(
        command,
        input=text.encode("utf-8"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=True,
        timeout=2,
    )


def selection_text(rows, anchor, end):
    if anchor is None or end is None or anchor == end:
        return ""
    first, last = sorted((anchor, end))
    parts = []
    for index in range(first[0], min(last[0] + 1, len(rows))):
        text = rows[index].get("copy_text")
        if text is None:
            continue
        left = first[1] if index == first[0] else 0
        right = last[1] if index == last[0] else len(text)
        parts.append(text[left:right])
    return "\n".join(parts).strip("\n")
