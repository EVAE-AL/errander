"""Enable `python -m errander`."""

import sys

from .cli import main

sys.exit(main())
