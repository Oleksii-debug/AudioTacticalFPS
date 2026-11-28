"""Compatibility entrypoint; canonical commands remain in operations.cli."""
from .operations.cli import main

if __name__ == '__main__':
    raise SystemExit(main())
