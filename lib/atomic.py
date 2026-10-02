"""atomic.py — replace a file's contents without ever exposing a partial write."""
import os
import pathlib
import tempfile


def write_text(path, text, encoding="utf-8"):
    """Write `text` to a temp file beside `path`, fsync it, then os.replace it over `path`.

    A crash or error at any point leaves the previous file intact; the temp file is removed.
    """
    path = pathlib.Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
