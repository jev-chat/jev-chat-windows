"""PyInstaller runtime hook: make PySide6 and Shiboken DLL directories visible."""
import os
import sys


if getattr(sys, "frozen", False):
    root = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    paths = [root, os.path.join(root, "PySide6"), os.path.join(root, "shiboken6")]
    for path in paths:
        if os.path.isdir(path):
            try:
                os.add_dll_directory(path)
            except (AttributeError, OSError):
                pass
    os.environ["PATH"] = os.pathsep.join(paths) + os.pathsep + os.environ.get("PATH", "")
