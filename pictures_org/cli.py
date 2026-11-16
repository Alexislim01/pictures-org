import argparse
import sys
from pathlib import Path

from .cleanup import make_plan, operate
from .scan import scan


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local photo/video inventory and recoverable exact-duplicate cleanup")
    commands = parser.add_subparsers(dest="command", required=True)
    scanner = commands.add_parser("scan", help="Read source files and save inventory/duplicate reports")
    scanner.add_argument("root", type=Path)
    scanner.add_argument("--workspace", type=Path, default=Path(".pictures-org"))
    scanner.add_argument("--fresh", action="store_true", help="Rehash all candidates instead of using saved hashes")
    planner = commands.add_parser("plan", help="Write a cleanup proposal without moving files")
    planner.add_argument("--report", type=Path, required=True)
    planner.add_argument("--quarantine", type=Path, required=True, help="Folder on the source drive, outside the source tree")
    planner.add_argument("--out", type=Path, required=True)
    planner.add_argument("--prefer", type=Path, help="Prefer keepers in this source subfolder")
    for name in ("apply", "restore", "purge"):
        command = commands.add_parser(name, help={"apply": "Move approved extra copies into quarantine",
                                                "restore": "Return quarantined files to their original paths",
                                                "purge": "Permanently delete quarantined copies"}[name])
        command.add_argument("--plan", type=Path, required=True)
        if name == "purge":
            command.add_argument("--confirm", required=True, help="Exact plan ID; permanent deletion")
    args = parser.parse_args(argv)
    progress = lambda message: print(message, flush=True)
    try:
        if args.command == "scan":
            report = scan(args.root, args.workspace, progress=progress, fresh=args.fresh)
            summary = report["summary"]
            print(f"\n{summary['files_scanned']:,} files; {summary['extra_copies']:,} extra copies; "
                  f"{summary['recoverable_bytes'] / 2**30:,.2f} GiB potential savings")
            print(f"Reports: {args.workspace.absolute()}")
            return 1 if summary["errors"] else 0
        if args.command == "plan":
            plan = make_plan(args.report, args.quarantine, args.out, args.prefer)
            actions = plan["actions"]
            print(f"Plan: {plan['id']}\nExtra media copies: {len(actions):,}\n"
                  f"Quarantine size: {sum(a['size_bytes'] for a in actions) / 2**30:,.2f} GiB")
            print(f"Review the source, keeper, and destination paths in: {args.out.absolute()}")
            print("No files moved. Quarantine alone does not free space on this drive.")
            for action in actions[:10]:
                print(f"  KEEP: {action['keeper']}\n  MOVE: {action['source']}")
            if len(actions) > 10:
                print(f"  ...and {len(actions) - 10:,} more actions in the plan")
            return 0
        count = operate(args.plan, args.command, getattr(args, "confirm", None), progress)
        print(f"{args.command}: {count:,} actions completed")
        if args.command == "apply":
            print("Copies are recoverable in quarantine. Disk space is freed only after purge.")
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted. Saved hashes and journal states are retained.", file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
