#!/usr/bin/env python3
"""Transcribe audio files using Whisper.
- ≤5s   → medium (short clips need accuracy)
- 6-15s → tiny for English, small for other languages
- 16-60s → small
- >60s  → medium
"""

import subprocess
import sys
import whisper

def get_duration(path):
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True
    )
    return float(result.stdout.strip())

def transcribe(path):
    duration = get_duration(path)

    if duration <= 5:
        model_name = "medium"
        sys.stderr.write(f"Duration: {duration:.1f}s → using medium (short clip)\n")
        sys.stderr.flush()
        model = whisper.load_model("medium", device="cpu")
    elif duration <= 15:
        # Detect language to decide tiny vs small
        tiny = whisper.load_model("tiny", device="cpu")
        audio = whisper.load_audio(path)
        audio = whisper.pad_or_trim(audio)
        mel = whisper.log_mel_spectrogram(audio).to("cpu")
        _, probs = tiny.detect_language(mel)
        lang = max(probs, key=probs.get)
        sys.stderr.write(f"Duration: {duration:.1f}s | Detected language: {lang}\n")
        sys.stderr.flush()

        if lang == "en":
            model_name = "tiny"
            model = tiny
        else:
            model_name = "small"
            model = whisper.load_model("small", device="cpu")
    elif duration <= 60:
        model_name = "small"
        model = whisper.load_model("small", device="cpu")
    else:
        model_name = "medium"
        model = whisper.load_model("medium", device="cpu")

    sys.stderr.write(f"Using {model_name} model\n")
    sys.stderr.flush()
    result = model.transcribe(path, fp16=False)
    return result["text"].strip()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: transcribe.py <audio_file>", file=sys.stderr)
        sys.exit(1)
    print(transcribe(sys.argv[1]))
