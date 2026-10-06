import subprocess

CMD = ["dotnet", "run", "--project", "App"]


def start():
    return subprocess.Popen(CMD, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
