#!/usr/bin/env python3
"""ROBOTIS AI Setup — Windows GUI entry point."""

import sys
import os

# Ensure the gui package is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.gui_app import run

if __name__ == "__main__":
    run()
