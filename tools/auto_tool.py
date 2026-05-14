import os

def create_tool(command, talk):

    if "create tool" in command:

        tool_name = command.replace("create tool", "").strip().replace(" ", "_")

        filename = f"tools/{tool_name}.py"

        if os.path.exists(filename):
            talk("Tool already exists")
            return True

        code = f'''
def run(command, talk):
    if "{tool_name.replace('_', ' ')}" in command:
        talk("Running {tool_name}")
        return True
    return False
'''

        with open(filename, "w") as f:
            f.write(code)

        talk(f"Created new tool {tool_name}")
        return True

    return False