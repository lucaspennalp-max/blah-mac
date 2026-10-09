import multiprocessing
import os
import sys

# Inside Blah.app, Python runs within Blah's own launcher. Helper processes that
# libraries start (multiprocessing, subprocess users) should use the real Python.
_python = os.environ.get("BLAH_PYTHON")
if _python and os.path.exists(_python):
    sys.executable = _python
    multiprocessing.set_executable(_python)

from .app import main  # noqa: E402

main()
