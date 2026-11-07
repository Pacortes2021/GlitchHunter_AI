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
import time
from pathlib import Path
from typing import Optional

# Asegurar que el directorio raíz del proyecto esté en sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from glitchhunter_ai.config import WEB_HOST, WEB_PORT
from glitchhunter_ai.core.database import PriceDatabase
from glitchhunter_ai.core.pipeline import GlitchHuntingPipeline
from glitchhunter_ai.scrapers import get_all_scrapers
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
    console.print("   [bold white]python3 run.py server[/bold white]")
    console.print("   Y abre en tu navegador: [underline green]http://127.0.0.1:8080[/underline green]\n")


def run_scan(live: bool = False, watchlist_only: bool = False, show_rotation: bool = False):
    print_banner()
    mode_str = "EN VIVO" if live else "SIMULACIÓN DE ALTA FIDELIDAD"
    pipeline = GlitchHuntingPipeline()

    if watchlist_only:
        console.print(f"[bold cyan]🎯 Ejecutando escaneo prioritario de WATCHLIST en modo: {mode_str}...[/bold cyan]\n")
        glitches = pipeline.run_watchlist_scan(use_live=live)
    else:
        console.print(f"[bold cyan]Ejecutando escaneo retail en modo: {mode_str}...[/bold cyan]\n")
        if show_rotation and pipeline.catalog_rotator:
            status = pipeline.catalog_rotator.get_status()
            console.print(
                f"🔄 [bold yellow]Rotación de Catálogo:[/bold yellow] Ciclo #{status['cycle_counter']} | "
                f"Estrategia: {status['current_strategy']} | "
                f"Categorías totales: {status['total_categories']}\n"
            )
        glitches = pipeline.run_scan(use_live=live)

    pipeline.print_glitch_summary_table(glitches)


def run_watchlist_cli(add_kw: Optional[str] = None, remove_kw: Optional[str] = None, list_kw: bool = False):
    print_banner()
    db = PriceDatabase()

    if add_kw:
        db.add_watchlist_keyword(add_kw)
        console.print(f"✅ [bold green]Palabra clave añadida exitosamente a la Watchlist:[/bold green] '{add_kw}'")
    elif remove_kw:
        db.remove_watchlist_keyword(remove_kw)
        console.print(f"🗑️ [bold yellow]Palabra clave eliminada de la Watchlist:[/bold yellow] '{remove_kw}'")

    keywords = db.get_watchlist_keywords()
    table = Table(title="⭐ Watchlist - Palabras Clave Prioritarias", show_header=True, header_style="bold cyan")
    table.add_column("#", justify="center", width=4)
    table.add_column("Término / Modelo Monitoreado", style="white")

    if not keywords:
        console.print("[dim yellow]No hay palabras clave registradas actualmente en tu Watchlist.[/dim yellow]")
        console.print("Usa: [bold white]python3 run.py watchlist --add 'rtx 4080'[/bold white] para agregar una.\n")
        return

    for idx, kw in enumerate(keywords, 1):
        table.add_row(str(idx), kw)

    console.print(table)
    console.print()


def run_tests():
    console.print("[bold cyan]Corriendo suite completa de pruebas unitarias con pytest...[/bold cyan]\n")
    res = subprocess.run([sys.executable, "-m", "pytest", "glitchhunter_ai/tests/", "-v"])
    sys.exit(res.returncode)


def run_diagnose(store_filter: Optional[str] = None):
    print_banner()
    console.print("[bold cyan]🩺 DIAGNÓSTICO EN VIVO DE CONECTIVIDAD Y SCRAPERS RETAIL[/bold cyan]")
    console.print("[dim]Verificando endpoints reales, latencias y extracción de datos en vivo de las tiendas...[/dim]\n")

    scrapers = get_all_scrapers()
    if store_filter:
        scrapers = [s for s in scrapers if s.store_id.lower() == store_filter.lower()]
        if not scrapers:
            console.print(f"[red]Error: Retailer '{store_filter}' no encontrado.[/red]")
            return

    table = Table(title="📊 Matriz de Diagnóstico en Tiempo Real", border_style="cyan")
    table.add_column("Retailer", style="bold white", width=18)
    table.add_column("ID", style="dim cyan", width=13)
    table.add_column("Estado", justify="center", width=14)
    table.add_column("Latencia", justify="right", width=11)
    table.add_column("Ítems", justify="right", width=9)
    table.add_column("Muestra de Producto Real / Diagnóstico", style="white", width=44)

    total_stores = len(scrapers)
    operational_stores = 0
    total_items = 0

    with console.status("[cyan]Sondeando retailers en vivo...[/cyan]"):
        for scraper in scrapers:
            s_id = scraper.store_id
            s_name = scraper.store_name
            t_start = time.perf_counter()
            items = []
            err_msg = None
            try:
                items = scraper.fetch_catalog(use_live=True)
            except Exception as exc:
                err_msg = str(exc)
            elapsed_ms = (time.perf_counter() - t_start) * 1000.0

            if items:
                operational_stores += 1
                total_items += len(items)
                status_badge = "[bold green]🟢 OPERATIVO[/bold green]"
                sample = items[0]
                price_clp = f"${sample.current_price:,.0f}".replace(",", ".")
                sample_desc = f"{sample.title[:26]}... ({price_clp})"
            elif err_msg:
                status_badge = "[bold red]🔴 ERROR[/bold red]"
                sample_desc = f"[red]{err_msg[:40]}[/red]"
            else:
                status_badge = "[bold yellow]🟡 VACÍO[/bold yellow]"
                sample_desc = "[yellow]0 productos devueltos (revisar maquetación)[/yellow]"

            table.add_row(
                s_name,
                s_id,
                status_badge,
                f"{elapsed_ms:,.0f} ms",
                str(len(items)),
                sample_desc,
            )

    console.print(table)
    console.print()

    rate = (operational_stores / total_stores) * 100.0 if total_stores else 0.0
    color = "green" if operational_stores == total_stores else "yellow"
    summary_panel = (
        f"🏪 [bold]Tiendas evaluadas:[/bold] {total_stores}\n"
        f"✅ [bold green]Tiendas operativas en vivo:[/bold green] {operational_stores}/{total_stores} ({rate:.1f}%)\n"
        f"📦 [bold]Total productos extraídos:[/bold] {total_items}\n"
        f"🛡️ [bold]Resiliencia:[/bold] Fallbacks de feed y evasión anti-bot activos"
    )
    console.print(Panel(summary_panel, title="🩺 Resumen del Diagnóstico de Red", border_style=color))


def main():
    parser = argparse.ArgumentParser(description="GlitchHunter AI - CLI Master")
    subparsers = parser.add_subparsers(dest="command", help="Comandos disponibles")

    # Comando demo
    subparsers.add_parser("demo", help="Demostración guiada completa con salida Rich en terminal")

    # Comando scan
    scan_parser = subparsers.add_parser("scan", help="Ejecutar ciclo de escaneo en retail chileno")
    scan_parser.add_argument("--live", action="store_true", help="Usar endpoints en vivo en lugar de simulación")
    scan_parser.add_argument("--watchlist", action="store_true", help="Escanear únicamente términos de la watchlist")
    scan_parser.add_argument("--rotate", action="store_true", help="Mostrar información detallada de la rotación de categorías")

    # Comando watchlist
    wl_parser = subparsers.add_parser("watchlist", help="Gestionar palabras clave monitoreadas por el usuario")
    wl_parser.add_argument("--list", action="store_true", help="Listar palabras clave activas")
    wl_parser.add_argument("--add", help="Agregar una nueva palabra clave (ej: 'rtx 4080', 'ipad air')")
    wl_parser.add_argument("--remove", help="Eliminar una palabra clave existente")

    # Comando server
    server_parser = subparsers.add_parser("server", help="Iniciar dashboard web en tiempo real con WebSockets")
    server_parser.add_argument("--host", default=WEB_HOST, help=f"Host (por defecto: {WEB_HOST})")
    server_parser.add_argument("--port", type=int, default=WEB_PORT, help=f"Puerto (por defecto: {WEB_PORT})")

    # Comando test
    subparsers.add_parser("test", help="Ejecutar suite de tests unitarios")
    subparsers.add_parser("retirar-falsos", help="Retirar del canal lo que no era una rebaja real (servicios, liquidaciones)")

    # Comando diagnose
    diagnose_parser = subparsers.add_parser("diagnose", help="Diagnosticar conectividad y extracción real en vivo de los retailers")
    diagnose_parser.add_argument("--store", help="Filtrar por ID de tienda específica (ej: falabella, solotodo)")

    args = parser.parse_args()

    if args.command == "demo" or args.command is None:
        run_demo()
    elif args.command == "scan":
        run_scan(live=args.live, watchlist_only=args.watchlist, show_rotation=args.rotate)
    elif args.command == "watchlist":
        run_watchlist_cli(add_kw=args.add, remove_kw=args.remove, list_kw=args.list)
    elif args.command == "server":
        print_banner()
        console.print(f"[bold green]🌐 Iniciando Radar Web en http://{args.host}:{args.port}[/bold green]")
        console.print("[dim]Presiona Ctrl+C para detener el servidor.[/dim]\n")
        start_server(host=args.host, port=args.port)
    elif args.command == "retirar-falsos":
        from glitchhunter_ai.core.pipeline import GlitchHuntingPipeline
        counts = GlitchHuntingPipeline().withdraw_false_offers()
        console.print(f"[green]Retiradas del canal:[/] {counts['servicios']} servicio(s), {counts['liquidaciones']} liquidación(es).")
    elif args.command == "test":
        run_tests()
    elif args.command == "diagnose":
        run_diagnose(store_filter=args.store)


if __name__ == "__main__":
    main()
