from harry_brain import (
    ask_harry,
    detect_intent,
    save_memory,
    get_memory,
    search_web,
    plan_task,
    create_tool,
    suggest_upgrade
)
import pyttsx3
import pywhatkit
import datetime
import wikipedia
import os
import difflib
import importlib
import time
import torch
from vosk import Model, KaldiRecognizer
import json

vosk_model = Model("vosk-model-en-us-0.22")

from wake_word import wait_for_wake_word

# ===== VOICE AUTHENTICATION =====

from resemblyzer import VoiceEncoder, preprocess_wav
import sounddevice as sd
import numpy as np
import pickle

# ===== TOOL LOADER SYSTEM =====

# ===== VOICE ACTIVITY DETECTION (VAD) =====

model, utils = torch.hub.load(
    repo_or_dir="snakers4/silero-vad", model="silero_vad", trust_repo=True
)

(get_speech_timestamps, save_audio, read_audio, VADIterator, collect_chunks) = utils

tools = []

tools_folder = "tools"

if os.path.exists(tools_folder):

    for file in os.listdir(tools_folder):

        if file.endswith(".py"):

            module_name = file[:-3]

            module = importlib.import_module(f"tools.{module_name}")

            tools.append(module)

# ===== LOAD VOICE PROFILE =====

encoder = VoiceEncoder()

with open("hareesh_voice_profile.pkl", "rb") as f:
    voice_profile = pickle.load(f)

engine = pyttsx3.init()

def talk(text):
    print("Harry:", text)
    engine.say(text)
    engine.runAndWait()

# ===== SMART WAKE WORD =====

def is_wake_word(text):

    words = text.split()

    for word in words:

        similarity = difflib.SequenceMatcher(None, word, "harry").ratio()

        if similarity > 0.6:  # important value
            return True

    return False

# ===== VERIFY VOICE[YTHON -M PIP] =====

def verify_voice():

    fs = 16000
    seconds = 3

    print("Checking voice...")

    recording = sd.rec(int(seconds * fs), samplerate=fs, channels=1, dtype="float32")
    sd.wait()

    wav = preprocess_wav(recording.flatten())

    embedding = encoder.embed_utterance(wav)

    similarity = np.dot(embedding, voice_profile)

    print("Voice similarity:", similarity)

    return similarity > 0.55

def listen():

    samplerate = 16000
    rec = KaldiRecognizer(vosk_model, samplerate)

    with sd.RawInputStream(
        samplerate=samplerate, blocksize=8000, dtype="int16", channels=1
    ) as stream:

        print("🎤 Listening (noise-safe)...")

        while True:
            data = stream.read(4000)[0]

            if rec.AcceptWaveform(data):
                result = rec.Result()
                text = json.loads(result)["text"]

                if text:
                    print("You said:", text)

                    text = text.lower()
                    text = text.replace("hey", "")
                    text = text.replace("harry", "")
                    text = text.strip()

                    return text

def open_app(app_name):
    talk("Opening " + app_name)
    os.system(f'start "" "{app_name}"')

def find_and_open_app(app_name):

    start_menu = [
        r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs",
        r"C:\Users\ADMIN\AppData\Roaming\Microsoft\Windows\Start Menu\Programs",
    ]

    for path in start_menu:
        for root, dirs, files in os.walk(path):
            for file in files:
                if app_name.lower() in file.lower() and file.endswith(".lnk"):
                    full_path = os.path.join(root, file)
                    talk("Opening " + app_name)
                    os.startfile(full_path)
                    return

    talk("I could not find that application")

def confirm_action(command):

    talk(f"Did you say {command}?")

    answer = listen() or ""

    if answer and "yes" in answer:
        return True
    else:
        talk("Okay cancelling command")
        return False
    
def load_tools():
    
    global tools
    
    tools = []    
    
    tools_folder = "tools"   
    
    if os.path.exists(tools_folder):     
        
        for file in os.listdir(tools_folder):    
            
            if file.endswith(".py"):              
                
                module_name = file[:-3]            
                
                try:          
                    module = importlib.import_module(f"tools.{module_name}")
                    
                    importlib.reload(module)                 
                    
                    tools.append(module)         
                except Exception as e:       
                    print("Tool load error:", e)

def run_harry(command):

    if command == "":
        return

    print("Command:", command)

    intent = detect_intent(command)
    print("AI Intent:", intent)

    if intent == "play_music":
        song = command.replace("play", "")
        talk("Playing " + song)
        pywhatkit.playonyt(song)

    elif intent == "tell_time":
        time = datetime.datetime.now().strftime("%I:%M %p")
        talk("Current time is " + time)

    elif "who is" in command:
        person = command.replace("who is", "")
        info = wikipedia.summary(person, 1)
        talk(info)

    elif "hello" in command:
        talk("Hello Hareesh. How can I help you?")

    elif "how are you" in command:
        talk("I'm doing great. Always ready to assist you.")

    elif "thank" in command:
        talk("You're welcome Hareesh. Happy to help.")

    elif "stop" in command:
        talk("Goodbye")
        exit()

    elif intent == "open_app":
        app = command.replace("open", "").strip()

        if confirm_action(command):
            find_and_open_app(app)

    elif "shutdown computer" in command:
        if confirm_action(command):
            talk("Shutting down the computer")
            os.system("shutdown /s /t 5")

    elif "remember" in command:

        text = command.replace("remember", "").strip()

        parts = text.split(" is ")

        if len(parts) == 2:
            key = parts[0]
            value = parts[1]

            save_memory(key, value)

            talk("Okay Hareesh, I will remember that.")

    elif "what is my" in command:

        key = command.replace("what is my", "").strip()

        value = get_memory(key)

        if value:
            talk(f"Your {key} is {value}")
        else:
            talk("I do not remember that yet.")

    elif "search" in command or "news" in command or "price" in command:

        talk("Searching the internet")

        result = search_web(command)

        talk(result[:200])

    elif "help me" in command or "do task" in command:

        talk("Planning your task")

        steps = plan_task(command)

        for step in steps:

            step = step.strip()

            if step == "":
                continue

            talk(f"Step: {step}")

            handled = False

            # Try tools first
            for tool in tools:
                if tool.run(step, talk):
                    handled = True
                    break
            # If no tool worked → fallback to AI
            if not handled:
                talk("I will handle this step intelligently")
                response = ask_harry(step)
                talk(response)
                
    elif "create tool" in command:
        task = command.replace("create tool", "").strip() 
        
        talk("Creating new tool") 
        
        result = create_tool(task) 
        
        talk(result)
    
    elif "upgrade yourself" in command:   
        
        talk("Analyzing my usage")  
        
        result = suggest_upgrade()  
        
        talk(result)

    # ===== CHECK TOOLS =====

    for tool in tools:

        handled = tool.run(command, talk)

        if handled:
            return

    else:
        answer = ask_harry(command)
        talk(answer)
        
if __name__ == "__main__":
    
    while True:
       
        if wait_for_wake_word():

            if verify_voice():
                talk("Hello Hareesh, I am listening continuously.")

                last_active_time = time.time()

                while True:
                    command = listen()

                    # If no speech, check timeout
                    if command == "":
                        if time.time() - last_active_time > 30:  # 30 seconds
                            talk("Going to sleep due to inactivity.")
                            break
                        continue

                    print("Command:", command)

                    last_active_time = time.time()

                    # EXIT COMMAND
                    if "stop listening" in command or "goodbye" in command:
                        talk("Okay Hareesh, going back to sleep.")
                        break

                    # NORMAL COMMAND PROCESS
                    run_harry(command)
            else:
                print("Voice not recognized.")

    time.sleep(1)