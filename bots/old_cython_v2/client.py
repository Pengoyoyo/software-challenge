#!/usr/bin/env python3
"""Wrapper to run old cython_v2 submission bot via benchmark.py --port interface."""
import sys
import os

# Point imports at the extracted submission's my_player directory
# so 'cython_core' resolves to the OLD compiled .so files
OLD_PLAYER = os.path.join(os.path.dirname(__file__), "my_player")
sys.path.insert(0, OLD_PLAYER)

# Now run the old logic.py as __main__
exec(open(os.path.join(OLD_PLAYER, "logic.py")).read())
