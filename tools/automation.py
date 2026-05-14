import os
import time

def run(command, talk):

    if "coding mode" in command:

        talk("Starting coding mode")

        talk("Opening VS Code")
        os.system("start code")

        time.sleep(2)

        talk("Opening Chrome")
        os.system("start chrome")

        time.sleep(2)

        talk("Opening GitHub")

        os.system("start https://github.com")

        return True

    return False