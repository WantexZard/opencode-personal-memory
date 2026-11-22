#!/usr/bin/env python3
"""Rule-only capture. The caller filters genuine main-session user events."""

import sys

sys.dont_write_bytecode = True

from memory_ops import SafeParser, capture, emit, load_input, run_safe  # noqa: E402


def main():
    parser = SafeParser(description=__doc__)
    parser.add_argument("--root")
    parser.add_argument("--event-json", action="store_true")
    parser.add_argument("message", nargs="?")
    args = parser.parse_args()
    if args.event_json:
        if args.message is not None:
            parser.error("invalid input")
        event = load_input()
    else:
        event = {
            "session_id": None,
            "message_id": None,
            "text": args.message if args.message is not None else sys.stdin.read(),
        }
    emit(capture(args.root, event))


if __name__ == "__main__":
    sys.exit(run_safe(main))
