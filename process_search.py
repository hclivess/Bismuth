"""Detect whether a process matching a name is currently running.

Small :mod:`psutil` helper used to check, by scanning every process command
line, whether a given process (e.g. ``node.py``) is alive. Run directly, it
prints whether ``node.py`` is currently running.
"""

import psutil


def proccess_presence(process_name):
    """Return ``True`` if ``process_name`` appears in any running process's command line."""
    for process in psutil.pids():
        try:
            p = psutil.Process(process)  # The pid of desired process

            # print(p.name()) # If the name is "python.exe" is called by python
            # print(p.cmdline()) # Is the command line this process has been called with
            if process_name in str(p.cmdline()):
                return True
        except:
            pass

    return False

if __name__ == "__main__":
    print (proccess_presence("node.py"))