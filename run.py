#!/usr/bin/env python3
"""
GlitchHunter AI - Entry Point Principal (CLI Maestro)

Comandos disponibles:
  python3 run.py server      Inicia el servidor web FastAPI y el Radar en http://127.0.0.1:8080
  python3 run.py scan        Ejecuta un ciclo de escaneo bajo demanda en retail chileno
  python3 run.py demo        Demostración interactiva con salida visual en terminal
  python3 run.py test        Ejecuta la suite completa de pruebas con pytest
"""

import sys
from pathlib import Path

# Garantiza que el directorio actual esté en sys.path para importar glitchhunter_ai
ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from glitchhunter_ai.run import main

if __name__ == "__main__":
    main()
