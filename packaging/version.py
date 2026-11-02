"""Prints the app version from web_app.py, for the build scripts."""

import re
from pathlib import Path

source = (Path(__file__).resolve().parent.parent / "web_app.py").read_text(encoding="utf-8")
print(re.search(r'^VERSION = "([^"]+)"', source, re.M).group(1))
