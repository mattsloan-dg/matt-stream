import argparse
import asyncio
import json
import logging
import os
import sys
import wave
from dataclasses import dataclass
from urllib.parse import urlencode

import websockets
from dotenv import load_dotenv

load_dotenv()

DEEPGRAM_WS_BASE = "wss://api.deepgram.com/v2/listen"


class JsonFormatter(logging.Formatter):
    # ANSI color codes
    GREEN = "\033[32m"
    RESET = "\033[0m"

    def format(self, record):
        log_entry = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        if hasattr(record, "extra"):
            log_entry.update(record.extra)

        json_output = json.dumps(log_entry, indent=2)

        message = record.getMessage()
        if "Connecting to Deepgram" in message:
            return f"{self.GREEN}{json_output}{self.RESET}\n"

        return f"{json_output}\n"


def setup_logger():
    logger = logging.getLogger("flux")
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    return logger


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

    if n_frames == 0 or len(audio_data) == 0 or duration_s == 0:
        raise ValueError(
            f"Audio file appears empty: frames={n_frames}, bytes={len(audio_data)}, duration_s={duration_s}"
        )

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


def build_ws_url(sample_rate, extra_params):
    params = {
        "encoding": "linear16",
        "sample_rate": sample_rate
    }
    for key, value in extra_params:
        params[key] = value
    return f"{DEEPGRAM_WS_BASE}?{urlencode(params)}"


async def send_audio(ws, audio_data, chunk_ms, bytes_per_ms, logger):
    chunk_bytes = int(bytes_per_ms * chunk_ms)
    offset = 0
    chunk_num = 0

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

        await ws.send(chunk)
        offset += chunk_bytes

        if is_last:
            last_chunk_ms = len(chunk) / bytes_per_ms
            await asyncio.sleep(last_chunk_ms / 1000)
        else:
            await asyncio.sleep(chunk_ms / 1000)

    await ws.send(json.dumps({"type": "CloseStream"}))
    logger.info("Sent CloseStream message")


async def receive_results(ws, logger, output_path=None):
    messages = []
    try:
        async for raw_msg in ws:
            msg = json.loads(raw_msg)
            if msg.get("type") == "TurnInfo":
                if not msg.get("transcript") and not msg.get("words"):
                    continue
            logger.info(
                "Message received",
                extra={"extra": {"raw": msg}},
            )
            messages.append(msg)
    except websockets.exceptions.ConnectionClosedOK:
        logger.info("WebSocket closed normally")
    except websockets.exceptions.ConnectionClosed as e:
        logger.warning(
            "WebSocket closed",
            extra={"extra": {"code": e.code, "reason": str(e.reason)}},
        )

    if output_path and messages:
        with open(output_path, "w") as f:
            json.dump(messages, f, indent=2)
        logger.info(
            "Messages written to file",
            extra={"extra": {"output_path": output_path, "message_count": len(messages)}},
        )


async def run(file_path, chunk_ms, dg_params, logger, output_path=None):
    api_key = os.environ.get("DEEPGRAM_API_KEY")
    if not api_key:
        logger.error("DEEPGRAM_API_KEY not set in environment or .env file")
        sys.exit(1)

    audio_data, sample_rate, channels, sample_width = read_wav(file_path, logger)

    bytes_per_second = sample_rate * channels * sample_width
    bytes_per_ms = bytes_per_second / 1000

    url = build_ws_url(sample_rate, dg_params)
    headers = {"Authorization": f"Token {api_key}"}

    logger.info(
        "Connecting to Deepgram",
        extra={
            "extra": {
                "url": url,
                "chunk_ms": chunk_ms,
                "chunk_bytes": int(bytes_per_ms * chunk_ms),
                "bytes_per_second": bytes_per_second,
            }
        },
    )

    async with websockets.connect(url, additional_headers=headers) as ws:
        sender = asyncio.create_task(
            send_audio(ws, audio_data, chunk_ms, bytes_per_ms, logger)
        )
        receiver = asyncio.create_task(
            receive_results(ws, logger, output_path)
        )
        await asyncio.gather(sender, receiver)

    logger.info("Session complete")


def parse_dg_param(value):
    if "=" not in value:
        raise argparse.ArgumentTypeError(
            f"Invalid format '{value}' — expected key=value"
        )
    return value.split("=", 1)


def main():
    parser = argparse.ArgumentParser(
        description="Stream a Linear16 WAV file to Deepgram and log all websocket messages"
    )
    parser.add_argument("file_path", help="Path to a Linear16 WAV file")
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=250,
        help="Chunk size in milliseconds (default: 250)",
    )
    parser.add_argument(
        "--dg-param",
        type=parse_dg_param,
        action="append",
        default=[],
        dest="dg_params",
        help="Deepgram query parameter as key=value (repeatable)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Path to write received messages as a JSON file (optional)",
    )
    args = parser.parse_args()

    logger = setup_logger()

    asyncio.run(run(args.file_path, args.chunk_size, args.dg_params, logger, args.output))


if __name__ == "__main__":
    main()
