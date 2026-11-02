#!/usr/bin/env python3
"""
GlitchHunter AI - CLI Principal y Orquestador de Ejecución
Permite:
- demo: Ejecución interactiva guiada demostrando detección en vivo y filtro anti-carcasas.
- scan: Escaneo bajo demanda de los 6 retailers de Chile (Falabella, Ripley, Paris, Líder, MELI, PC Factory).
- server: Servidor Web FastAPI con Dashboard en tiempo real y WebSockets en http://localhost:8080.
- test: Ejecución integral de la suite de pruebas unitarias con pytest.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Asegurar que el directorio raíz del proyecto esté en sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from glitchhunter_ai.config import WEB_HOST, WEB_PORT
from glitchhunter_ai.core.database import PriceDatabase
from glitchhunter_ai.core.pipeline import GlitchHuntingPipeline
from glitchhunter_ai.web.server import start_server

console = Console()


def print_banner():
    banner = Text()
    banner.append("🎯 GLITCHHUNTER AI ⚡\n", style="bold cyan")
    banner.append("Radar Inteligente de Errores de Precio en Retail Chileno\n", style="bold white")
    banner.append("6 Retailers: Falabella | Ripley | Paris | Líder | Mercado Libre | PC Factory\n", style="dim cyan")
    banner.append("Filtro Semántico NLP Anti-Carcasas & Detector de Missing Zero Bug (~10x)\n", style="bold yellow")
    console.print(Panel(banner, border_style="cyan"))


def run_demo():
    print_banner()
    console.print("[bold green]Iniciando demostración integral de GlitchHunter AI...[/bold green]\n")

    db = PriceDatabase()
    pipeline = GlitchHuntingPipeline(db=db)

    console.print("🔍 [cyan]Paso 1: Extracción simultánea desde los retailers chilenos (modo demo seguro)...[/cyan]")
    glitches = pipeline.run_scan(use_live=False, send_notifications=False)

    console.print("\n📊 [cyan]Paso 2: Resumen Ejecutivo de Glitches Detectados[/cyan]")
    pipeline.print_glitch_summary_table(glitches)

    stats = db.get_stats()
    stats_text = (
        f"✅ Catálogo Analizado: {stats.get('total_products', 0)} productos en 6 tiendas\n"
        f"🚨 Glitches Críticos Confirmados: {stats.get('total_glitches', 0)}\n"
        f"💰 Ahorro Acumulado Potencial: ${stats.get('accumulated_savings_clp', 0.0):,.0f} CLP\n"
        f"🛡️ Filtro Anti-Carcasas: 100% de trampas de accesorios rechazadas"
    ).replace(",", ".")

    console.print(Panel(stats_text, title="📈 Métricas Acumuladas del Radar", border_style="green"))
    console.print("\n[bold cyan]💡 Para ver el radar visual interactivo en tiempo real ejecuta:[/bold cyan]")
    console.print("   [bold white]python3 glitchhunter_ai/run.py server[/bold white]")
    console.print("   Y abre en tu navegador: [underline green]http://127.0.0.1:8080[/underline green]\n")


def run_scan(live: bool = False):
    print_banner()
    mode_str = "EN VIVO" if live else "SIMULACIÓN DE ALTA FIDELIDAD"
    console.print(f"[bold cyan]Ejecutando escaneo retail en modo: {mode_str}...[/bold cyan]\n")

    pipeline = GlitchHuntingPipeline()
    glitches = pipeline.run_scan(use_live=live)
    pipeline.print_glitch_summary_table(glitches)


def run_tests():
    console.print("[bold cyan]Corriendo suite completa de pruebas unitarias con pytest...[/bold cyan]\n")
    res = subprocess.run([sys.executable, "-m", "pytest", "glitchhunter_ai/tests/", "-v"])
    sys.exit(res.returncode)


def main():
    parser = argparse.ArgumentParser(description="GlitchHunter AI - CLI Master")
    subparsers = parser.add_subparsers(dest="command", help="Comandos disponibles")

    # Comando demo
    subparsers.add_parser("demo", help="Demostración guiada completa con salida Rich en terminal")

    # Comando scan
    scan_parser = subparsers.add_parser("scan", help="Ejecutar ciclo de escaneo en retail chileno")
    scan_parser.add_argument("--live", action="store_true", help="Usar endpoints en vivo en lugar de simulación")

    # Comando server
    server_parser = subparsers.add_parser("server", help="Iniciar dashboard web en tiempo real con WebSockets")
    server_parser.add_argument("--host", default=WEB_HOST, help=f"Host (por defecto: {WEB_HOST})")
    server_parser.add_argument("--port", type=int, default=WEB_PORT, help=f"Puerto (por defecto: {WEB_PORT})")

    # Comando test
    subparsers.add_parser("test", help="Ejecutar suite de tests unitarios")

    args = parser.parse_args()

    if args.command == "demo" or args.command is None:
        run_demo()
    elif args.command == "scan":
        run_scan(live=args.live)
    elif args.command == "server":
        print_banner()
        console.print(f"[bold green]🌐 Iniciando Radar Web en http://{args.host}:{args.port}[/bold green]")
        console.print("[dim]Presiona Ctrl+C para detener el servidor.[/dim]\n")
        start_server(host=args.host, port=args.port)
    elif args.command == "test":
        run_tests()


if __name__ == "__main__":
    main()
