#!/usr/bin/env python3
"""Compatibility entry point for the renamed Dev Mesh Observer launcher."""

from observer import main


if __name__ == "__main__":
    raise SystemExit(main())
