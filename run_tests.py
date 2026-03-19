"""Runner script to execute stream_audio.py 5 times for each audio file and collect results."""
import json
import re
import subprocess
import sys

AUDIO_FILES = ["nao_1.wav", "sim_1.wav", "sim_2.wav", "sim_success.wav"]
RUNS_PER_FILE = 5
BASE_CMD = [
    "uv", "run", "python", "stream_audio.py",
    "--dg-param", "model=nova-3",
    "--dg-param", "language=pt-BR",
    "--dg-param", "interim_results=true",
    "--dg-param", "punctuate=true",
    "--chunk-size", "80",
]

results = []

for audio_file in AUDIO_FILES:
    for i in range(RUNS_PER_FILE):
        cmd = BASE_CMD + [f"./{audio_file}"]
        print(f"Running: {audio_file} (iteration {i + 1}/{RUNS_PER_FILE})", file=sys.stderr)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        stdout = proc.stdout
        # Find all JSON blocks between ======================= markers
        blocks = re.findall(r"={23}\n(.*?)\n={23}", stdout, re.DOTALL)

        last_transcript = ""
        request_id = ""

        for block in blocks:
            try:
                msg = json.loads(block)
                transcript = (
                    msg.get("channel", {})
                    .get("alternatives", [{}])[0]
                    .get("transcript", "")
                )
                rid = msg.get("metadata", {}).get("request_id", "")
                if transcript:
                    last_transcript = transcript
                if rid:
                    request_id = rid
            except json.JSONDecodeError:
                pass

        results.append({
            "audio_file": audio_file,
            "transcript": last_transcript,
            "request_id": request_id,
        })
        print(
            f"  -> request_id: {request_id}, transcript: {last_transcript[:80]}...",
            file=sys.stderr,
        )

# Output results as JSON to stdout
print(json.dumps(results, indent=2))
