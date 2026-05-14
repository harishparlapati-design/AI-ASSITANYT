from harry_brain import search_web, ask_harry

def run(command, talk):

    if "research" in command:

        topic = command.replace("research", "").strip()

        talk("Researching " + topic)

        # step 1 search internet
        results = search_web(topic)

        talk("Analyzing information")

        # step 2 summarize using AI
        summary = ask_harry(
            f"Summarize this information in simple words: {results}"
        )

        talk(summary)

        return True

    return False