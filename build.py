"""Build a single-file FileHunter app for this OS with PyInstaller.

    pip install pyinstaller pillow
    python build.py

The result lands in dist/.
"""
import os
import subprocess
import sys

here = os.path.dirname(os.path.abspath(__file__))
icon = os.path.join(here, "assets", "icon.ico" if sys.platform == "win32" else "icon.png")
args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
        "--name", "FileHunter", "--icon", icon, "--hidden-import", "PIL._tkinter_finder",
        "--hidden-import", "filehunter_people", "--hidden-import", "filehunter_faces"]
if sys.platform != "darwin":
    args.append("--onefile")
args.append(os.path.join(here, "filehunter.py"))
sys.exit(subprocess.call(args, cwd=here))
