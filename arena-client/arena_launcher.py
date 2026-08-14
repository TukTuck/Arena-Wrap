"""Reproducible, fail-closed launcher for the Arena Hermes Desktop client."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from arena_api import ArenaControl
from arena_archive import ArchiveError, archive_local_diagnostics
from arena_reports import render_provider_health_report
from arena_runtime import ArenaRuntimeError, RuntimeConfig
from arena_version import VERSION_TAG
from arena_transport import ExternalLiveRequestGate


DEFAULT_CONFIG = Path(__file__).with_name("arena-config.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Startet den isolierten Arena Hermes Desktop")
    parser.add_argument("--version", action="version", version=VERSION_TAG)
    parser.add_argument(
        "command",
        nargs="?",
        choices=("start", "diagnostics", "report", "archive"),
        default="start",
        help="start, diagnostics, report oder archive",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Explizite Arena-Konfiguration (kein globaler Fallback)",
    )
    parser.add_argument("--check", action="store_true", help="Nur Runtime validieren, nichts starten")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Runtime, HTTP, WebSocket-Gate und Arena-Metadaten prüfen, danach stoppen",
    )
    parser.add_argument("--timeout", type=float, default=45.0, help="Ready-Timeout in Sekunden")
    parser.add_argument("--json", action="store_true", help="Diagnose als JSON ausgeben")
    parser.add_argument(
        "--window", choices=("1h", "6h", "24h", "7d"), default="1h",
        help="Lokales Report-Zeitfenster (report)",
    )
    parser.add_argument(
        "--format", dest="output_format", choices=("text", "json"), default="text",
        help="Reportformat: text oder json (report)",
    )
    parser.add_argument(
        "--output", type=Path,
        help="Lokale Reportdatei oder Archivverzeichnis; vorhandene Ziele werden nicht überschrieben",
    )
    parser.add_argument("--history", action="store_true", help="Health-History archivieren (archive)")
    parser.add_argument("--alerts", action="store_true", help="Alert-State archivieren (archive)")
    parser.add_argument("--all", dest="archive_all", action="store_true", help="History und Alerts archivieren (archive)")
    parser.add_argument(
        "--provider",
        action="append",
        dest="providers",
        help="Provider für diagnostics auswählen; mehrfach verwendbar",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Diagnose ohne Netzwerkzugriff (Standard für diagnostics)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Expliziten gated Health-Check ausführen",
    )
    parser.add_argument(
        "--reason",
        help="Begründung für --live; erforderlich für die Live-Freigabe",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "diagnostics":
        return _run_diagnostics(args)
    if args.command == "report":
        return _run_report(args)
    if args.command == "archive":
        return _run_archive(args)
    if (
        args.providers or args.dry_run or args.live or args.reason
        or args.output or args.history or args.alerts or args.archive_all
        or args.output_format != "text" or args.window != "1h"
    ):
        print("ARENA ARGUMENT ERROR: Provider-/Reportoptionen benötigen den passenden Unterbefehl.", file=sys.stderr)
        return 2
    try:
        config = RuntimeConfig.load(args.config)
        config.validate()
        if args.check:
            _print({"mode": "check", "runtime": config.as_public_dict()}, args.json)
            return 0

        control = ArenaControl(config)
        runtime = control.start(args.timeout)
        try:
            result = {
                "mode": "smoke" if args.smoke else "start",
                "runtime_status": runtime,
                "websocket": "desktop-internal-authenticated-probe",
                "model_request": "NOT EXECUTED",
            }
            if args.smoke:
                result["arena_product"] = control.smoke(args.timeout)
            _print(result, args.json)
            return 0
        finally:
            if args.smoke:
                control.stop()
    except ArenaRuntimeError as exc:
        print(f"ARENA RUNTIME ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def _run_report(args: argparse.Namespace) -> int:
    if (
        args.check or args.smoke or args.dry_run or args.live or args.reason or args.json
        or args.history or args.alerts or args.archive_all
    ):
        print("ARENA ARGUMENT ERROR: report unterstützt nur lokale Reportoptionen.", file=sys.stderr)
        return 2
    if args.providers and len(args.providers) > 1:
        print("ARENA ARGUMENT ERROR: report akzeptiert genau einen Provider.", file=sys.stderr)
        return 2
    try:
        config = RuntimeConfig.load(args.config)
        # Reports are read-only local operations; no Hermes runtime validation
        # or process startup is needed for them.
        control = ArenaControl(config)
        provider = args.providers[0] if args.providers else None
        report = control.provider_health_report(
            window=args.window,
            provider=provider,
        )
        if args.output:
            destination = args.output.expanduser().resolve()
            if destination.exists():
                print(f"ARENA REPORT ERROR: Ziel existiert bereits: {destination}", file=sys.stderr)
                return 1
            control.export_provider_health_report(
                destination,
                window=args.window,
                provider=provider,
                format="json" if args.output_format == "json" else "txt",
            )
            print(f"Report geschrieben: {destination}")
        elif args.output_format == "json":
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print(render_provider_health_report(report), end="")
        return 0
    except (ArenaRuntimeError, KeyError, ValueError, OSError) as exc:
        print(f"ARENA REPORT ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def _run_archive(args: argparse.Namespace) -> int:
    if args.check or args.smoke or args.providers or args.dry_run or args.live or args.reason or args.json or args.output_format != "text" or args.window != "1h":
        print("ARENA ARGUMENT ERROR: archive unterstützt nur --history, --alerts, --all und --output.", file=sys.stderr)
        return 2
    if args.archive_all and (args.history or args.alerts):
        print("ARENA ARGUMENT ERROR: --all darf nicht mit --history/--alerts kombiniert werden.", file=sys.stderr)
        return 2
    try:
        config = RuntimeConfig.load(args.config)
        # Archive rotates only local JSON state and must remain usable when the
        # isolated Desktop is currently unavailable.
        control = ArenaControl(config)
        archive_all = args.archive_all or not (args.history or args.alerts)
        output_dir = args.output or (config.arena_state_dir / "archive")
        paths = archive_local_diagnostics(
            control.health_history,
            control.alert_states,
            output_dir,
            history_enabled=archive_all or args.history,
            alerts_enabled=archive_all or args.alerts,
        )
        for path in paths:
            print(f"Archiv geschrieben: {path}")
        return 0
    except (ArchiveError, FileExistsError, OSError, ValueError, KeyError) as exc:
        print(f"ARENA ARCHIVE ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def _run_diagnostics(args: argparse.Namespace) -> int:
    if args.check or args.smoke:
        print("ARENA ARGUMENT ERROR: diagnostics kann nicht mit --check/--smoke kombiniert werden.", file=sys.stderr)
        return 2
    if args.live and args.dry_run:
        print("ARENA ARGUMENT ERROR: --live und --dry-run sind exklusiv.", file=sys.stderr)
        return 2
    if args.output or args.output_format != "text" or args.history or args.alerts or args.archive_all or args.window != "1h":
        print("ARENA ARGUMENT ERROR: diagnostics unterstützt diese Optionen nicht.", file=sys.stderr)
        return 2
    if args.reason and not args.live:
        print("ARENA ARGUMENT ERROR: --reason benötigt --live.", file=sys.stderr)
        return 2
    if args.live and not str(args.reason or "").strip():
        print("ARENA LIVE GATE ERROR: --live benötigt eine explizite --reason.", file=sys.stderr)
        return 3

    try:
        config = RuntimeConfig.load(args.config)
        config.validate()
        control = ArenaControl(config)
        gate = ExternalLiveRequestGate.explicit(args.reason) if args.live else None
        result = control.provider_diagnostics(args.providers, live_gate=gate)
        _print_diagnostics(result, args.json)
        if not args.live:
            return 0
        checked = result.get("checked_providers", [])
        if not checked:
            return 1
        states = {
            item["health"]
            for item in result["providers"]
            if item["provider"] in checked
        }
        return 0 if states == {"healthy"} else 1
    except (ArenaRuntimeError, KeyError) as exc:
        print(f"ARENA DIAGNOSTICS ERROR: {exc}", file=sys.stderr)
        return 1 if args.live else 2
    except KeyboardInterrupt:
        return 130


def _print_diagnostics(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    print("Arena Provider Diagnostics")
    print("=========================")
    print(f"Mode: {payload['mode']}")
    print(f"Live Gate: {payload['live_gate']}")
    print(f"Network Requests: {payload['network_requests']}")
    for item in payload["providers"]:
        print(f"\n{item['name']} ({item['provider']})")
        print(f"  adapter: {'available' if item['adapter_available'] else 'unavailable'}")
        print(f"  configured: {'yes' if item['configured'] else 'no'}")
        print(f"  credential: {item['credential_status']}")
        print(f"  health: {item['health']}")
        print(f"  circuit_breaker: {item['circuit_breaker']['state']}\n"
              f"  failure_count: {item['circuit_breaker']['failure_count']}")
        print(f"  models: {', '.join(item['models']) if item['models'] else '-'}")
        print(f"  retry_after: {item['retry_after_seconds'] if item['retry_after_seconds'] is not None else '-'}")
        print(f"  network: {item['network']}")
        if item["last_error"]:
            print(f"  last_error: {item['last_error']}")


def _print(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    print("Arena Application: PASS")
    if payload.get("mode") == "check":
        print("Konfiguration und isolierte Pfade sind gültig.")
        return
    runtime = payload["runtime_status"]
    print(f"Runtime: {runtime['status']}")
    print(f"Desktop PID: {runtime['pid']}")
    print(f"Backend-Port: {runtime['backend_port']}")
    print("HTTP: " + ", ".join(f"{key}={value}" for key, value in runtime["http"].items()))
    print("WebSocket: durch den Hermes-Desktop-internen authentifizierten Probe geprüft")
    if "arena_product" in payload:
        product = payload["arena_product"]
        print(
            "Arena State: "
            f"projects={product['state_after_cleanup']['projects']}, "
            f"sessions={product['state_after_cleanup']['sessions']}, "
            f"agents={product['state_after_cleanup']['agents']}"
        )
        registry = product.get("provider_registry", {})
        print(
            "Provider Registry: "
            f"count={registry.get('count', 0)}, "
            f"configured={registry.get('configured_count', 0)}, "
            f"not_configured={len(registry.get('not_configured', []))}"
        )
    print("Model Request: NOT EXECUTED")


if __name__ == "__main__":
    raise SystemExit(main())
