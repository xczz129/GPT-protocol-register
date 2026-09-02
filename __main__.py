# -*- coding: utf-8 -*-
"""python -m gpt-register-fp  (run from parent) or python __main__.py"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cli import main

if __name__ == "__main__":
    main()
