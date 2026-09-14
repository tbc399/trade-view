#!/usr/bin/env python
import argparse
import getpass
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from trade_view.auth import hash_password  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a salted PASSWORD hash for Trade View."
    )
    parser.add_argument("password", nargs="?", help="Plain-text password to hash.")
    args = parser.parse_args()

    password = args.password or getpass.getpass("Password: ")
    print(hash_password(password))


if __name__ == "__main__":
    main()
