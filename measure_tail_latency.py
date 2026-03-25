"""Measure tail latency of Deepgram streaming transcription.

Streams a WAV file to Deepgram and measures the time between starting a timer
at a configurable offset into the audio and receiving the last transcript result.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field

import websockets
from dotenv import load_dotenv

from stream_audio import build_ws_url, parse_dg_param, read_wav, setup_logger

load_dotenv()


@dataclass
class TailLatencyState:
    """State for tracking tail latency measurement."""

    timer_started: asyncio.Event = field(default_factory=asyncio.Event)
    timer_start_time: float = 0.0
    measured_elapsed: float | None = None
    send_finished: asyncio.Event = field(default_factory=asyncio.Event)


async def send_audio(
    ws: websockets.WebSocketClientProtocol,
    audio_data: bytes,
    chunk_ms: int,
    bytes_per_ms: float,
    finalize: bool,
    timer_offset_s: float,
    state: TailLatencyState,
    logger: logging.Logger,
) -> None:
    """Send audio chunks to Deepgram with tail latency timer and optional Finalize messages."""
    chunk_bytes = int(bytes_per_ms * chunk_ms)
    offset = 0
    chunk_num = 0
    timer_trigger_bytes = int(bytes_per_ms * timer_offset_s * 1000)
    finalize_counter = 0

    while offset < len(audio_data):
        chunk = audio_data[offset : offset + chunk_bytes]
        chunk_num += 1
        is_last = (offset + chunk_bytes) >= len(audio_data)

        await ws.send(chunk)
        offset += chunk_bytes

        # Start timer at configured offset (once)
        if not state.timer_started.is_set() and offset >= timer_trigger_bytes:
            state.timer_start_time = time.perf_counter()
            state.timer_started.set()
            logger.info(f"Timer started at {timer_offset_s}s mark — measuring tail latency")

        # Send Finalize message every 4 iterations (once per second at 250ms) after threshold
        if finalize and offset >= timer_trigger_bytes:
            finalize_counter += 1
            if finalize_counter % 4 == 1:
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


async def receive_results(
    ws: websockets.WebSocketClientProtocol,
    state: TailLatencyState,
    logger: logging.Logger,
) -> None:
    """Receive transcription results and track tail latency."""
    try:
        async for raw_msg in ws:
            msg = json.loads(raw_msg)
            msg_type = msg.get("type", "")

            if msg_type == "Results":
                channel = msg.get("channel", {})
                alternatives = channel.get("alternatives", [{}])
                transcript = alternatives[0].get("transcript", "") if alternatives else ""
                is_final = msg.get("is_final", False)

                if transcript.strip():
                    logger.info(
                        "Transcript received",
                        extra={
                            "extra": {
                                "is_final": is_final,
                                "transcript": transcript,
                            }
                        },
                    )

                    # Update tail latency tracking
                    if state.timer_started.is_set():
                        state.measured_elapsed = time.perf_counter() - state.timer_start_time

            elif msg_type == "Metadata":
                logger.info(
                    "Metadata received",
                    extra={"extra": {"request_id": msg.get("request_id", "")}},
                )
            else:
                logger.debug(
                    "Message received",
                    extra={"extra": {"type": msg_type}},
                )
    except websockets.exceptions.ConnectionClosedOK:
        logger.info("WebSocket closed normally")
    except websockets.exceptions.ConnectionClosed as e:
        logger.warning(
            "WebSocket closed",
            extra={"extra": {"code": e.code, "reason": str(e.reason)}},
        )


async def run(
    file_path: str,
    chunk_ms: int,
    finalize: bool,
    timer_offset_s: float,
    dg_params: list[tuple[str, str]],
    logger: logging.Logger,
) -> None:
    """Stream audio to Deepgram and measure tail latency."""
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
        "Connecting to Deepgram for tail latency measurement",
        extra={
            "extra": {
                "url": url,
                "chunk_ms": chunk_ms,
                "finalize": finalize,
                "timer_offset_s": timer_offset_s,
            }
        },
    )

    state = TailLatencyState()

    try:
        async with websockets.connect(url, additional_headers=headers) as ws:
            sender = asyncio.create_task(
                send_audio(ws, audio_data, chunk_ms, bytes_per_ms, finalize, timer_offset_s, state, logger)
            )
            receiver = asyncio.create_task(
                receive_results(ws, state, logger)
            )
            await asyncio.gather(sender, receiver)
    except asyncio.CancelledError:
        logger.info("Stream interrupted")

    # Log tail latency results
    summary: dict[str, object] = {
        "finalize": finalize,
        "chunk_ms": chunk_ms,
        "timer_offset_s": timer_offset_s,
    }

    if state.measured_elapsed is not None:
        summary["tail_latency_s"] = round(state.measured_elapsed, 4)
        logger.info("Tail latency measurement complete", extra={"extra": summary})

        if state.measured_elapsed > 2.5:
            logger.warning(
                "Tail latency exceeded threshold",
                extra={
                    "extra": {
                        "tail_latency_s": round(state.measured_elapsed, 4),
                        "threshold_s": 2.5,
                        "exceeded_by_s": round(state.measured_elapsed - 2.5, 4),
                    }
                },
            )
    else:
        logger.warning("No transcript received after timer started", extra={"extra": summary})


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Measure tail latency of Deepgram streaming transcription"
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
        help="Send periodic Finalize messages after the timer offset",
    )
    parser.add_argument(
        "--timer-offset",
        type=float,
        default=10.0,
        dest="timer_offset_s",
        help="Seconds into the audio to start the tail latency timer (default: 10)",
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

    try:
        asyncio.run(
            run(args.file_path, args.chunk_size, args.finalize, args.timer_offset_s, args.dg_params, logger)
        )
    except KeyboardInterrupt:
        logger.info("Interrupted by user")


if __name__ == "__main__":
    main()
