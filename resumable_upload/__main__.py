"""Allow `python -m resumable_upload` to dispatch into the CLI."""

import sys

from resumable_upload.cli import main

if __name__ == "__main__":
    sys.exit(main())
