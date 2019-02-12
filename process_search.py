import psutil


def proccess_presence(process_name):
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