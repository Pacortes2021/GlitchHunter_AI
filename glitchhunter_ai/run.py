#!/usr/bin/env python3
"""
GlitchHunter AI - Módulo de compatibilidad retrocompatible.
El CLI principal ahora reside en glitchhunter_ai.cli.
"""

from glitchhunter_ai.cli import (
    main,
    print_banner,
    run_demo,
    run_scan,
    run_watchlist_cli,
    run_diagnose,
    run_tests,
    get_all_scrapers,
)

__all__ = [
    "main",
    "print_banner",
    "run_demo",
    "run_scan",
    "run_watchlist_cli",
    "run_diagnose",
    "run_tests",
    "get_all_scrapers",
]

if __name__ == "__main__":
    main()
