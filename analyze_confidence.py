"""
Finds the 5 lowest-confidence words across all EndOfTurn events in a results file
and reports the full transcript + the low-confidence word(s) for each.

Usage:
    python analyze_confidence.py results.json
    python analyze_confidence.py results.json --word "the"
"""

import argparse
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find the 5 lowest-confidence words in EndOfTurn events."
    )
    parser.add_argument(
        "file",
        type=Path,
        help="Path to the results JSON file",
    )
    parser.add_argument(
        "--word",
        type=str,
        default=None,
        help="If provided, only examine occurrences of this specific word (case-insensitive)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if not args.file.exists():
        logger.error("File not found: %s", args.file)
        sys.exit(1)

    with args.file.open() as f:
        data = json.load(f)

    # Collect word entries from EndOfTurn events, carrying the transcript.
    # Each entry: {"word": str, "confidence": float, "transcript": str}
    word_entries: list[dict] = []
    eot_count = 0

    for obj in data:
        if obj.get("type") != "TurnInfo" or obj.get("event") != "EndOfTurn":
            continue

        eot_count += 1
        transcript = obj.get("transcript", "")

        for word_obj in obj.get("words", []):
            if args.word and word_obj["word"].lower() != args.word.lower():
                continue
            word_entries.append(
                {
                    "word": word_obj["word"],
                    "confidence": word_obj["confidence"],
                    "transcript": transcript,
                }
            )

    if not word_entries:
        if args.word:
            logger.warning('No occurrences of "%s" found in EndOfTurn events', args.word)
        else:
            logger.warning("No EndOfTurn events with words found")
        return

    label = f'"{args.word}"' if args.word else "all words"
    logger.info(
        "Scanned %d occurrence(s) of %s across %d EndOfTurn event(s)",
        len(word_entries),
        label,
        eot_count,
    )

    bottom_5 = sorted(word_entries, key=lambda e: e["confidence"])[:5]

    header = f'5 LOWEST CONFIDENCE: {label.upper()}'
    print("\n" + "=" * 60)
    print(header)
    print("=" * 60)

    for rank, entry in enumerate(bottom_5, start=1):
        print(f"\n#{rank}  confidence: {entry['confidence']:.4f}")
        print(f"    word:       \"{entry['word']}\"")
        print(f"    transcript: \"{entry['transcript']}\"")

    print("\n" + "=" * 60)


if __name__ == "__main__":
    main()
