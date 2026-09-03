import sounddevice as sd
import numpy as np

# Pi 4: Seeed ReSpeaker; Pi 5: CD002-AUDIO (speaker), GENERAL WEBCAM (mic)
OUTPUT_NAMES = ["seeed", "cd002"]
INPUT_NAMES = ["seeed", "webcam"]


def get_audio_device(output=True):
    """Return the first matching audio device index for output or input."""
    names = OUTPUT_NAMES if output else INPUT_NAMES
    for i, d in enumerate(sd.query_devices()):
        name = d["name"].lower()
        if output and d["max_output_channels"] > 0 and any(k in name for k in names):
            return i
        if not output and d["max_input_channels"] > 0 and any(k in name for k in names):
            return i
    return None


audio_output = get_audio_device(output=True)
audio_input = get_audio_device(output=False)

if audio_output is None:
    # This file is a hands-on hardware script, not a unit test: importing it
    # plays a tone and records from the mic. pytest still collects it, so on a
    # machine with no ReSpeaker/CD002 the old `raise` here aborted COLLECTION
    # and took the whole suite down with it — `make hal-test` could not run at
    # all off-device. Skip instead; running it directly
    # (`python test/test_audio.py`) is unchanged and still fails loudly.
    try:
        import pytest

        pytest.skip(
            "no %s output device — audio smoke test needs a real body" % OUTPUT_NAMES,
            allow_module_level=True,
        )
    except ImportError:
        raise RuntimeError(
            "No output audio device found! (looked for: %s)" % OUTPUT_NAMES
        )

# Use device native sample rate
dev_info = sd.query_devices(audio_output)
sample_rate = int(dev_info["default_samplerate"])
print(f"Output device: {dev_info['name']} (rate={sample_rate})")

# --- Test Speaker ---
duration = 3  # seconds
print("Playing test tone...")
frequency = 440  # Hz (A4 note)
t = np.linspace(0, duration, int(sample_rate * duration), endpoint=False)
tone = 0.5 * np.sin(2 * np.pi * frequency * t).astype(np.float32)
sd.play(tone, samplerate=sample_rate, device=audio_output)
sd.wait()

# --- Test Microphone ---
if audio_input is not None:
    in_info = sd.query_devices(audio_input)
    in_rate = int(in_info["default_samplerate"])
    print(f"Input device: {in_info['name']} (rate={in_rate})")
    print("Recording from microphone...")
    recording = sd.rec(int(duration * in_rate), samplerate=in_rate,
                       channels=1, device=audio_input)
    sd.wait()
    print("Recording complete.")

    # --- Playback Recorded Audio ---
    print("Playing back recorded audio...")
    sd.play(recording, samplerate=sample_rate, device=audio_output)
    sd.wait()
else:
    print("No input device found (mic may use ALSA default), skipping record test.")

print("Done.")
