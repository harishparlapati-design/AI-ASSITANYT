def run(command, talk):

    if "shutdown computer" in command:
        
        talk("Shutting down computer")
        
        import os
        os.system("shutdown /s /t 5")
        
        return True

    return False