import sys
import os
import pathlib

script_path = pathlib.Path(__file__).resolve()
project_root = script_path.parent.parent.parent
venv_python = project_root / ".venv" / "bin" / "python"

if sys.prefix == sys.base_prefix and venv_python.exists():
    try:
        os.execv(str(venv_python), [str(venv_python)] + sys.argv)
    except OSError as e:
        print(f"[WARN] Failed to bootstrap virtual environment: {e}")

import warnings
import logging

warnings.filterwarnings("ignore", category=UserWarning, module="google.genai")
logging.getLogger("google_genai").setLevel(logging.ERROR)

from importlib.metadata import version, PackageNotFoundError

__version__ = "0.13.3"

try:
    __version__ = version("deepresearch")
except PackageNotFoundError:
    pass

from deepresearch.__main__ import main as main
