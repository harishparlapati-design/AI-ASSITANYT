import requests

def run(command, talk):

    if "weather" in command:

        try:

            city = command.replace("weather in", "")
            city = city.replace("weather", "").strip()

            url = f"https://wttr.in/{city}?format=3"

            response = requests.get(url)

            weather = response.text

            talk(weather)

            return True

        except:
            talk("Sorry, I could not get the weather")

            return True

    return False