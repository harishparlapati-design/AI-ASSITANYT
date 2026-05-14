conversation_history = []

from duckduckgo_search import DDGS

import ollama

SYSTEM_PROMPT = """
You are Harry, a friendly and intelligent personal AI assistant.
You speak in a calm, polite and slightly humorous tone.
You help the user with tasks, information and coding.
Keep answers short and natural like a human assistant.
"""


def ask_harry(question):

    history = load_conversation()

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    # 🔥 memory search
    memory = search_memory(question)

    if memory:
        messages.append({"role": "system", "content": f"Past memory: {memory}"})
        
    # recent conversation
    for item in history[-20:]:
        messages.append({"role": item["role"], "content": item["text"]})
        
    # current question   
    messages.append({"role": "user", "content": question})  
    
    # 🔥 NOW call AI
    response = ollama.chat(
        model="llama3",
        messages=messages    )   
    
    reply = response["message"]["content"]   
    
    save_conversation("user", question)  
    save_conversation("assistant", reply)  
    
    return reply


def detect_intent(command):

    prompt = f"""
    Classify the user command.

    Possible intents:
    open_app
    play_music
    tell_time
    general_question

    Command: {command}

    Respond with only the intent name.
    """

    response = ollama.chat(
        model="llama3", messages=[{"role": "user", "content": prompt}]
    )

    return response["message"]["content"].strip()


import json

MEMORY_FILE = "memory.json"


def save_memory(key, value):

    try:
        with open(MEMORY_FILE, "r") as f:
            memory = json.load(f)
    except:
        memory = {}

    memory[key] = value

    with open(MEMORY_FILE, "w") as f:
        json.dump(memory, f)


def get_memory(key):

    try:
        with open(MEMORY_FILE, "r") as f:
            memory = json.load(f)

        return memory.get(key, None)

    except:
        return None


def search_web(query):

    results = []

    with DDGS() as ddgs:
        for r in ddgs.text(query, max_results=3):
            results.append(r["body"])

    return " ".join(results)


def plan_task(command):
    import ollama

    prompt = f"""    
    Break this task into simple steps.    
    
    Task: {command}    
    Rules:    
    - Each step in one line    
    - Use simple commands like: open chrome, search python, open vs code    """

    response = ollama.chat(
        model="llama3", messages=[{"role": "user", "content": prompt}]
    )
    steps = response["message"]["content"].split("\n")

    return steps


def create_tool(task):
    import ollama
    import os

    prompt = f"""    
    Create a Python tool for this task:    
    
    {task}    
    
    Rules:    
    - Must have function: run(command, talk)    
    - Return True if command handled    
    - Safe code only    
    - No explanation, only code    
    """

    response = ollama.chat(
        model="llama3", messages=[{"role": "user", "content": prompt}]
    )
    code = response["message"]["content"]

    filename = task.replace(" ", "_") + ".py"

    path = os.path.join("tools", filename)

    with open(path, "w", encoding="utf-8") as f:
        f.write(code)

        return f"Tool created: {filename}"

import json

# 🔹 LOAD CONVERSATION
def load_conversation():
    try:
        with open("conversation_memory.json", "r") as f:
            return json.load(f)
    except:
        return []

# 🔹 SAVE CONVERSATION
def save_conversation(role, text):

    try:
        with open("conversation_memory.json", "r") as f:
            data = json.load(f)
    except:
        data = []

    data.append({"role": role, "text": text})

    with open("conversation_memory.json", "w") as f:
        json.dump(data, f, indent=2)

# 🔹 SEARCH MEMORY
def search_memory(query):

    try:
        with open("conversation_memory.json", "r") as f:
            data = json.load(f)
    except:
        return ""

    results = []

    for item in data:
        if query.lower() in item["text"].lower():
            results.append(item["text"])

    return " ".join(results[-5:])

def analyze_usage():

    try:
        with open("command_history.json", "r") as f:
            commands = json.load(f)
    except:
        return "No data to analyze"

    # simple frequency check
    freq = {}

    for cmd in commands:
        word = cmd.split()[0]

        freq[word] = freq.get(word, 0) + 1

    # find most used
    sorted_cmds = sorted(freq.items(), key=lambda x: x[1], reverse=True)

    top = sorted_cmds[:3]

    return f"Most used commands: {top}"

def suggest_upgrade():

    usage = analyze_usage()

    prompt = f"""
    Based on this usage:

    {usage}

    Suggest new features or tools to improve the assistant.
    Keep it short.
    """

    response = ollama.chat(
        model="llama3",
        messages=[{"role": "user", "content": prompt}]
    )

    return response["message"]["content"]