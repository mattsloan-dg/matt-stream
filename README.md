# matt-stream

Tools for streaming audio to the Deepgram API and measuring transcription latency and confidence metrics. There are lots of starter projects already out there. I just like this one because I built it from scratch exactly the way I wanted it. It's all just personal preference though.

## Setup

Requires Python 3.11+.

```bash
# Create virtual environment and install dependencies
uv sync

# Copy .env template and add your Deepgram API key
cp .env.example .env
```

Set `DEEPGRAM_API_KEY` in `.env` with your key.

## Scripts

### stream_audio.py

Stream a Linear16 WAV file to Deepgram and measure tail latency + time-to-first-transcript. The tail latency timer starts at a configurable offset into the audio stream.

```bash
python stream_audio.py <file_path> [options]
```

| Option                 | Description                                    | Default |
| ---------------------- | ---------------------------------------------- | ------- |
| `--chunk-size INT`     | Chunk size in milliseconds                     | `250`   |
| `--finalize`           | Send Finalize message after last audio chunk   | off     |
| `--timer-offset FLOAT` | Seconds into audio to start tail latency timer | `10`    |
| `--dg-param KEY=VALUE` | Deepgram query parameter (repeatable)          | none    |

```bash
# Basic usage
python stream_audio.py audio.wav

# Custom model with finalize enabled
python stream_audio.py audio.wav --dg-param model=nova-3 --finalize

# Smaller chunks with 5s timer offset
python stream_audio.py audio.wav --chunk-size 100 --timer-offset 5
```

---

### stream_audio_last_chunk.py

Similar to `stream_audio.py`, but the tail latency timer starts after the last audio chunk is sent. Supports repeated runs with statistical summaries.

```bash
python stream_audio_last_chunk.py <file_path> [options]
```

| Option                 | Description                                  | Default |
| ---------------------- | -------------------------------------------- | ------- |
| `--chunk-size INT`     | Chunk size in milliseconds                   | `250`   |
| `--finalize`           | Send Finalize message after last audio chunk | off     |
| `--dg-param KEY=VALUE` | Deepgram query parameter (repeatable)        | none    |
| `--repeat INT`         | Number of iterations (2-99)                  | `1`     |

```bash
# Single run
python stream_audio_last_chunk.py audio.wav

# 10 repeated runs with summary statistics
python stream_audio_last_chunk.py audio.wav --repeat 10 --dg-param model=nova-3
```

---

### flux.py

Stream audio to the Deepgram **v2** API and collect all WebSocket messages. No latency measurement — useful for inspecting raw API responses.

```bash
python flux.py <file_path> [options]
```

| Option                 | Description                                | Default |
| ---------------------- | ------------------------------------------ | ------- |
| `--chunk-size INT`     | Chunk size in milliseconds                 | `250`   |
| `--dg-param KEY=VALUE` | Deepgram query parameter (repeatable)      | none    |
| `--output PATH`        | Write all received messages to a JSON file | none    |

```bash
# Stream and save all messages to file
python flux.py audio.wav --output results.json --dg-param model=nova-3
```

---

### analyze_confidence.py

Analyze transcription confidence scores from a JSON results file (output from `flux.py`).

```bash
python analyze_confidence.py <file> [options]
```

| Option          | Description                                                    | Default   |
| --------------- | -------------------------------------------------------------- | --------- |
| `--word STRING` | Only examine occurrences of a specific word (case-insensitive) | all words |

```bash
# Analyze all words
python analyze_confidence.py results.json

# Filter to a specific word
python analyze_confidence.py results.json --word "hello"
```

---

### loop_stream_audio.py

Run `stream_audio.py` in an infinite loop for stress testing. Ctrl+C to stop.

```bash
python loop_stream_audio.py <file_path> [options]
```

| Option                 | Description                                  | Default |
| ---------------------- | -------------------------------------------- | ------- |
| `--chunk-size INT`     | Chunk size in milliseconds                   | `250`   |
| `--finalize`           | Send Finalize message after last audio chunk | off     |
| `--dg-param KEY=VALUE` | Deepgram query parameter (repeatable)        | none    |
| `--delay FLOAT`        | Delay between iterations in seconds          | `1.0`   |

```bash
# Loop with 2s delay between runs
python loop_stream_audio.py audio.wav --delay 2.0 --dg-param model=nova-3
```

## Audio Format

All scripts expect **Linear16 PCM WAV** files. The sample rate and channels are read from the WAV header and passed to the Deepgram API automatically. Please convert the audio to .wav format first before using any of the scripts. Recommend `ffmpeg` for this.

## Dependencies

- `websockets` - Direct WebSocket connections to Deepgram
- `python-dotenv` - Load API key from `.env`
