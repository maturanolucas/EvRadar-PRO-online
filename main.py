#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EvRadar PRO entrypoint (flat multi-file layout).

Run:
  python -u main.py

This file simply delegates to evradar_monolith.py (your full bot code).
"""

from evradar_monolith import main

if __name__ == "__main__":
    main()
