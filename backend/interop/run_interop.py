#!/usr/bin/env python3
"""Single entry point for the AP2 interop run.

    python interop/run_interop.py
    python interop/run_interop.py --base https://your-tunnel.ngrok-free.dev
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from interop.driver import main

if __name__ == "__main__":
    sys.exit(main())
