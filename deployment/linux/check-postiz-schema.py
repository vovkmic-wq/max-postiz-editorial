#!/usr/bin/env python3
"""Inspect immutable image engines without starting Postiz or connecting to a DB."""

from postiz_schema_check import main

if __name__ == "__main__":
    raise SystemExit(main())
