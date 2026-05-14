import sounddevice as sd
from scipy.io.wavfile import write
import os

fs = 44100
seconds = 4

folder = "voice_samples"

if not os.path.exists(folder):
    os.makedirs(folder)

for i in range(1, 11):
    input("Press ENTER and speak sample " + str(i))
    recording = sd.rec(int(seconds * fs), samplerate=fs, channels=1)
    sd.wait()
    write(f"{folder}/sample{i}.wav", fs, recording)
    print("Saved sample", i)

print("Voice recording finished")