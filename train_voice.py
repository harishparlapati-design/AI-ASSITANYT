from resemblyzer import VoiceEncoder, preprocess_wav
import numpy as np
import os
import pickle

encoder = VoiceEncoder()

folder = "voice_samples"
embeddings = []

for file in os.listdir(folder):
    if file.endswith(".wav"):
        wav = preprocess_wav(os.path.join(folder, file))
        embed = encoder.embed_utterance(wav)
        embeddings.append(embed)

voice_profile = np.mean(embeddings, axis=0)

with open("hareesh_voice_profile.pkl", "wb") as f:
    pickle.dump(voice_profile, f)

print("Voice training completed. Profile saved.")