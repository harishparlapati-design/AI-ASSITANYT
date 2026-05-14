def run(command, talk):

    if "calculate" in command:

        try:
            text = command.replace("calculate", "").strip()

            text = text.replace("plus", "+")
            text = text.replace("minus", "-")
            text = text.replace("times", "*")
            text = text.replace("multiplied by", "*")
            text = text.replace("divided by", "/")

            result = eval(text)

            talk(f"The answer is {result}")

            return True

        except:
            talk("Sorry, I could not calculate that")

            return True

    return False