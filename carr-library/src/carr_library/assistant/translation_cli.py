"""Compatibility import for the one source-aware reviewed translation CLI.

The implementation and explicit remote approval gates remain in translation_cli.
No independent provider, credentials, job store, or command executor is created.
"""
from ..translation_cli import main

__all__ = ['main']


if __name__ == '__main__':
    raise SystemExit(main())
