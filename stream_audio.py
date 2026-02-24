import argparse
import asyncio
import json
import logging
import os
import sys
import time
import wave
from dataclasses import dataclass, field
from urllib.parse import urlencode

import websockets
from dotenv import load_dotenv

load_dotenv()

DEEPGRAM_WS_BASE = "wss://api.sandbox.deepgram.com/v1/listen"


class JsonFormatter(logging.Formatter):
    # ANSI color codes
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    RED = "\033[31m"
    RESET = "\033[0m"

    def format(self, record):
        log_entry = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        if hasattr(record, "extra"):
            log_entry.update(record.extra)

        json_output = json.dumps(log_entry)

        # Add color codes based on message content
        message = record.getMessage()
        if "Connecting to Deepgram" in message:
            return f"{self.GREEN}{json_output}{self.RESET}"
        elif "Timer started at 11s mark" in message:
            return f"{self.YELLOW}{json_output}{self.RESET}"
        elif "First transcript received" in message or "Started sending audio" in message:
            return f"{self.CYAN}{json_output}{self.RESET}"
        elif "Tail latency exceeded threshold" in message:
            return f"{self.RED}{json_output}{self.RESET}"

        return json_output


def setup_logger():
    logger = logging.getLogger("stream_audio")
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    return logger


@dataclass
class StreamState:
    timer_started: asyncio.Event = field(default_factory=asyncio.Event)
    timer_start_time: float = 0.0
    measured_elapsed: float | None = None
    send_finished: asyncio.Event = field(default_factory=asyncio.Event)
    connection_start_time: float = 0.0
    first_transcript_time: float | None = None


def read_wav(file_path, logger):
    with wave.open(file_path, "rb") as wf:
        sample_rate = wf.getframerate()
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        comptype = wf.getcomptype()
        n_frames = wf.getnframes()
        audio_data = wf.readframes(n_frames)

    if sample_width != 2:
        raise ValueError(
            f"Expected Linear16 (sample_width=2), got sample_width={sample_width}"
        )
    if comptype != "NONE":
        raise ValueError(
            f"Expected uncompressed audio (comptype='NONE'), got comptype='{comptype}'"
        )

    duration_s = n_frames / sample_rate
    logger.info(
        "WAV file loaded",
        extra={
            "extra": {
                "file_path": file_path,
                "sample_rate": sample_rate,
                "channels": channels,
                "sample_width": sample_width,
                "frames": n_frames,
                "duration_s": round(duration_s, 3),
                "total_bytes": len(audio_data),
            }
        },
    )
    return audio_data, sample_rate, channels, sample_width


def build_ws_url(sample_rate, channels, extra_params):
    params = {
        "encoding": "linear16",
        "sample_rate": sample_rate,
        "channels": channels,
    }
    for key, value in extra_params:
        params[key] = value
    return f"{DEEPGRAM_WS_BASE}?{urlencode(params)}"


async def send_audio(ws, audio_data, chunk_ms, bytes_per_ms, finalize, state, logger):
    chunk_bytes = int(bytes_per_ms * chunk_ms)
    offset = 0
    chunk_num = 0
    timer_trigger_bytes = int(bytes_per_ms * 11000)  # 11 seconds in bytes
    finalize_counter = 0  # Track iterations after threshold for periodic Finalize

    while offset < len(audio_data):
        chunk = audio_data[offset : offset + chunk_bytes]
        chunk_num += 1
        is_last = (offset + chunk_bytes) >= len(audio_data)

        logger.debug(
            "Sending chunk",
            extra={
                "extra": {
                    "chunk_num": chunk_num,
                    "chunk_bytes": len(chunk),
                    "is_last": is_last,
                }
            },
        )

        # Start time-to-first-transcript timer on first chunk
        if chunk_num == 1:
            state.connection_start_time = time.perf_counter()
            logger.info("Started sending audio — measuring time to first transcript")

        await ws.send(chunk)
        offset += chunk_bytes

        # Start timer at 10 seconds in (once)
        if not state.timer_started.is_set() and offset >= timer_trigger_bytes:
            state.timer_start_time = time.perf_counter()
            state.timer_started.set()
            logger.info("Timer started at 11s mark — measuring tail latency")

        # Send Finalize message every 4 iterations (once per second at 250ms) after threshold
        if finalize and offset >= timer_trigger_bytes:
            finalize_counter += 1
            if finalize_counter % 4 == 1:  # Send on 1st, 5th, 9th, etc. iteration after threshold
                await ws.send(json.dumps({"type": "Finalize"}))
                logger.info("Sent Finalize message")

        if is_last:
            last_chunk_ms = len(chunk) / bytes_per_ms
            await asyncio.sleep(last_chunk_ms / 1000)
        else:
            await asyncio.sleep(chunk_ms / 1000)
   
    await ws.send(json.dumps({"type": "CloseStream"}))
    logger.info("Sent CloseStream message")
    state.send_finished.set()


async def receive_results(ws, state, logger):
    try:
        async for raw_msg in ws:
            msg = json.loads(raw_msg)
            msg_type = msg.get("type", "")

            if msg_type == "Results":
                channel = msg.get("channel", {})
                alternatives = channel.get("alternatives", [{}])
                transcript = alternatives[0].get("transcript", "") if alternatives else ""
                is_final = msg.get("is_final", False)
                speech_final = msg.get("speech_final", False)
                from_finalize = msg.get("from_finalize", False)

                log_data = {
                    "extra": {
                        "type": msg_type,
                        "is_final": is_final,
                        "speech_final": speech_final,
                        "from_finalize": from_finalize,
                        "transcript": transcript,
                    }
                }

                # Measure time to first transcript (any Results message)
                if state.first_transcript_time is None and state.connection_start_time > 0:
                    time_to_first = time.perf_counter() - state.connection_start_time
                    state.first_transcript_time = time_to_first
                    log_data["extra"]["time_to_first_transcript_s"] = round(time_to_first, 4)
                    logger.info("First transcript received", extra=log_data)
                # Measure tail latency: update for ANY non-empty transcript after timer started
                elif state.timer_started.is_set() and transcript.strip():
                    elapsed = time.perf_counter() - state.timer_start_time
                    is_first_measurement = state.measured_elapsed is None
                    state.measured_elapsed = elapsed
                    log_data["extra"]["tail_latency_s"] = round(elapsed, 4)

                    if is_first_measurement:
                        logger.info("Tail latency measured on is_final", extra=log_data)
                    else:
                        log_data["extra"]["updated"] = True
                        logger.info("Tail latency updated - non-empty trailing transcript", extra=log_data)
                elif transcript.strip():
                    logger.info("Transcript received", extra=log_data)

            elif msg_type == "Metadata":
                logger.info(
                    "Metadata received",
                    extra={"extra": {"type": msg_type, "request_id": msg.get("request_id", "")}},
                )
            elif msg_type == "UtteranceEnd":
                logger.info(
                    "Utterance End received: ", msg
                )
                await ws.send(json.dumps({"type": "Finalize"}))
                logger.info("Sent Finalize message")
            else:
                logger.debug(
                    "Message received",
                    extra={"extra": {"type": msg_type, "raw": msg}},
                )
    except websockets.exceptions.ConnectionClosedOK:
        logger.info("WebSocket closed normally")
    except websockets.exceptions.ConnectionClosed as e:
        logger.warning(
            "WebSocket closed",
            extra={"extra": {"code": e.code, "reason": str(e.reason)}},
        )


async def run(file_path, chunk_ms, finalize, dg_params, logger):
    api_key = os.environ.get("DEEPGRAM_API_KEY")
    if not api_key:
        logger.error("DEEPGRAM_API_KEY not set in environment or .env file")
        sys.exit(1)

    audio_data, sample_rate, channels, sample_width = read_wav(file_path, logger)

    bytes_per_second = sample_rate * channels * sample_width
    bytes_per_ms = bytes_per_second / 1000

    url = build_ws_url(sample_rate, channels, dg_params)
    headers = {"Authorization": f"Token {api_key}"}

    logger.info(
        "Connecting to Deepgram",
        extra={
            "extra": {
                "url": url,
                "chunk_ms": chunk_ms,
                "chunk_bytes": int(bytes_per_ms * chunk_ms),
                "finalize": finalize,
                "bytes_per_second": bytes_per_second,
            }
        },
    )

    state = StreamState()

    async with websockets.connect(url, additional_headers=headers) as ws:
        sender = asyncio.create_task(
            send_audio(ws, audio_data, chunk_ms, bytes_per_ms, finalize, state, logger)
        )
        receiver = asyncio.create_task(
            receive_results(ws, state, logger)
        )
        await asyncio.gather(sender, receiver)

    # Log final summary with all metrics
    summary = {
        "finalize": finalize,
        "chunk_ms": chunk_ms,
    }

    if state.first_transcript_time is not None:
        summary["time_to_first_transcript_s"] = round(state.first_transcript_time, 4)

    if state.measured_elapsed is not None:
        summary["tail_latency_s"] = round(state.measured_elapsed, 4)
        logger.info("Session complete", extra={"extra": summary})

        # Check if tail latency exceeds threshold
        if state.measured_elapsed > 2.5:
            logger.warning(
                "Tail latency exceeded threshold",
                extra={
                    "extra": {
                        "tail_latency_s": round(state.measured_elapsed, 4),
                        "threshold_s": 2.5,
                        "exceeded_by_s": round(state.measured_elapsed - 2.5, 4),
                    }
                }
            )
    else:
        logger.warning("No is_final message received after timer started", extra={"extra": summary})


def parse_dg_param(value):
    if "=" not in value:
        raise argparse.ArgumentTypeError(
            f"Invalid format '{value}' — expected key=value"
        )
    return value.split("=", 1)


def main():
    parser = argparse.ArgumentParser(
        description="Stream a Linear16 WAV file to Deepgram and measure tail latency"
    )
    parser.add_argument("file_path", help="Path to a Linear16 WAV file")
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=250,
        help="Chunk size in milliseconds (default: 250)",
    )
    parser.add_argument(
        "--finalize",
        action="store_true",
        help="Send a Finalize message after the last audio chunk",
    )
    parser.add_argument(
        "--dg-param",
        type=parse_dg_param,
        action="append",
        default=[],
        dest="dg_params",
        help="Deepgram query parameter as key=value (repeatable)",
    )
    args = parser.parse_args()

    logger = setup_logger()

    asyncio.run(run(args.file_path, args.chunk_size, args.finalize, args.dg_params, logger))


if __name__ == "__main__":
    main()
