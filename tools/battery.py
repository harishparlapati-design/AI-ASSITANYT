import psutil

def run(command, talk):

    if "battery" in command:

        battery = psutil.sensors_battery()

        percent = battery.percent

        talk(f"Battery is {percent} percent")

        return True

    return False