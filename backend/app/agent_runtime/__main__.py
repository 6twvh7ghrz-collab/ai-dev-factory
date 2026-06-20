"""CLI entrypoint for the local agent runtime harness."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .runtime_config import RuntimeConfig
from .runtime_service import AgentRuntimeService


def _load_config(path: str | None, args) -> RuntimeConfig:
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    else:
        data = {}
    if args.mode:
        data["mode"] = args.mode
    if args.control_plane_url:
        data["control_plane_url"] = args.control_plane_url
    if args.worker_id:
        data["worker_id"] = args.worker_id
    if args.sandbox_root:
        data["sandbox_root"] = args.sandbox_root
    if args.allowed_task_id:
        data["allowed_task_ids"] = [int(args.allowed_task_id)]
    if args.max_cycles is not None:
        data["max_cycles"] = int(args.max_cycles)
    return RuntimeConfig.from_dict(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local V2 agent runtime harness")
    parser.add_argument("command", choices=["start", "run-one", "stop", "status"])
    parser.add_argument("--config", default=None)
    parser.add_argument("--mode", default=None)
    parser.add_argument("--control-plane-url", default=None)
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--sandbox-root", default=None)
    parser.add_argument("--allowed-task-id", type=int, default=None)
    parser.add_argument("--max-cycles", type=int, default=None)
    args = parser.parse_args(argv)

    try:
        config = _load_config(args.config, args)
        if args.command == "status":
            if config.status_file.exists():
                print(config.status_file.read_text(encoding="utf-8"))
            else:
                state = AgentRuntimeService(config).status()
                print(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.command == "stop":
            config.stop_file.write_text("stop", encoding="utf-8")
            print(json.dumps({"ok": True, "stop_file": str(config.stop_file)}, ensure_ascii=False))
            return 0

        runtime = AgentRuntimeService(config)
        if args.command == "run-one":
            result = runtime.run_one_cycle()
            runtime.shutdown()
        else:
            result = runtime.run_forever()
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.get("ok") else 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error_code": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
