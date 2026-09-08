"""Shared command-line validation and error presentation."""
import argparse
import sys

def fail(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    return 1

def port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer between 1 and 65535") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be an integer between 1 and 65535")
    return port

