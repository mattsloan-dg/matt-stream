#!/usr/bin/env python3
import argparse
import json
import logging
import subprocess
import sys
import time


class JsonFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        if hasattr(record, "extra"):
            log_entry.update(record.extra)
        return json.dumps(log_entry)


def setup_logger():
    logger = logging.getLogger("loop_stream_audio")
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    return logger


def main():
    parser = argparse.ArgumentParser(
        description="Run stream_audio.py in an infinite loop"
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
        action="append",
        default=[],
        dest="dg_params",
        help="Deepgram query parameter as key=value (repeatable)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Delay in seconds between iterations (default: 1.0)",
    )
    args = parser.parse_args()

    logger = setup_logger()

    # Build the command to run stream_audio.py
    cmd = ["uv", "run", "python", "stream_audio.py", args.file_path]

    if args.chunk_size != 250:
        cmd.extend(["--chunk-size", str(args.chunk_size)])

    if args.finalize:
        cmd.append("--finalize")

    for param in args.dg_params:
        cmd.extend(["--dg-param", param])

    logger.info(
        "Starting infinite loop",
        extra={
            "extra": {
                "command": " ".join(cmd),
                "delay_seconds": args.delay,
            }
        },
    )

    iteration = 0
    while True:
        iteration += 1
        logger.info(
            "Starting iteration",
            extra={"extra": {"iteration": iteration}},
        )

        try:
            result = subprocess.run(
                cmd,
                capture_output=False,
                check=True,
            )
            logger.info(
                "Iteration completed successfully",
                extra={"extra": {"iteration": iteration, "return_code": result.returncode}},
            )
        except subprocess.CalledProcessError as e:
            logger.error(
                "Iteration failed",
                extra={
                    "extra": {
                        "iteration": iteration,
                        "return_code": e.returncode,
                        "error": str(e),
                    }
                },
            )
        except KeyboardInterrupt:
            logger.info(
                "Loop interrupted by user",
                extra={"extra": {"total_iterations": iteration}},
            )
            sys.exit(0)
        except Exception as e:
            logger.error(
                "Unexpected error",
                extra={
                    "extra": {
                        "iteration": iteration,
                        "error": str(e),
                        "error_type": type(e).__name__,
                    }
                },
            )

        if args.delay > 0:
            logger.info(
                "Waiting before next iteration",
                extra={"extra": {"delay_seconds": args.delay}},
            )
            time.sleep(args.delay)


if __name__ == "__main__":
    main()
