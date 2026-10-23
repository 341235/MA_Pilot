"""CLI commands for acquisition, checks and pilot runs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import load_config
from .pipeline import fetch_references, run
from .storage import json_value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lp_pilot")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "fetch-reference", "fetch-fine-reference", "fetch-rpc", "prepare-observations", "validate-features"):
        item = sub.add_parser(command)
        item.add_argument("--config", default="configs/pilot.toml")
        if command == "prepare-observations":
            item.add_argument("--allow-network", action="store_true", help="Fetch only missing required historical headers within existing budgets")
    args = parser.parse_args(argv)
    try:
        cfg = load_config(args.config)
        if args.command == "fetch-reference":
            result = fetch_references(cfg)
        elif args.command == "fetch-fine-reference":
            from .fine_reference import fetch_fine_references
            result = fetch_fine_references(cfg)
        elif args.command == "validate-features":
            from .feature_validation import validate_features
            result = validate_features(cfg)
            result = {key: result[key] for key in ("status", "fine_status", "fine_error", "network_requests", "variant_counts", "minute_formula_regression") if key in result}
            result["report"] = str(Path(cfg["_root"]) / "reports/feature_validation.md")
        elif args.command == "prepare-observations":
            from .observation_headers import prepare_observation_headers
            result = prepare_observation_headers(cfg, allow_network=args.allow_network)
            result = {key: result[key] for key in ("status", "path", "sha256", "summary", "usage", "error") if key in result}
        elif args.command == "fetch-rpc":
            from .acquire import fetch_rpc
            import time
            last_update = [0.0]
            def progress(value):
                now = time.monotonic()
                if now - last_update[0] > 20:
                    print(json.dumps({"progress": value}), flush=True)
                    last_update[0] = now
            result = fetch_rpc(cfg, progress=progress)
        else:
            result = run(cfg)
        print(json.dumps(json_value(result), indent=2, sort_keys=True))
        return 2 if result.get("status") in {"BLOCKED", "FAIL"} else 0
    except (ValueError, OSError, RuntimeError) as exc:
        # Provider errors must already be sanitized by the access adapter.
        print(json.dumps({"status": "FAIL", "error_type": type(exc).__name__, "detail": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
