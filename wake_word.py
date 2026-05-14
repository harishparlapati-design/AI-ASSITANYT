import pvporcupine
import pyaudio
import struct
import os

def wait_for_wake_word():

    access_key = os.getenv("PICO_VOICE_ACCESS_KEY")
    if not access_key:
        raise RuntimeError(
            "Missing PICO_VOICE_ACCESS_KEY environment variable for Porcupine wake word."
        )

    porcupine = pvporcupine.create(
        access_key=access_key,
        
        keyword_paths=["hay-harry_en_windows_v4_0_0.ppn"]
    )
    pa = pyaudio.PyAudio()

    audio_stream = pa.open(
        rate=porcupine.sample_rate,
        channels=1,
        format=pyaudio.paInt16,
        input=True,
        frames_per_buffer=porcupine.frame_length
    )

    print("Waiting for wake word...")

    while True:

        pcm = audio_stream.read(porcupine.frame_length)
        pcm = struct.unpack_from("h" * porcupine.frame_length, pcm)

        result = porcupine.process(pcm)

        if result >= 0:
            print("Wake word detected!")
            return True