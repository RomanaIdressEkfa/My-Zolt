import sys

# Windows consoles default to a legacy code page; make Bangla text print correctly.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

from .bot import main  # noqa: E402

main()
