#!/usr/bin/env python3
"""Run selected account-onboarding steps without letting one account stop the chain."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORKFLOW = ROOT / "instagram_web_profile_workflow.py"
SCHEDULER = ROOT / "connection_scheduler.py"
ALLOWED = {"create_profiles", "auto_login", "check_login"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--accounts", required=True)
    parser.add_argument("--tasks", required=True)
    parser.add_argument("--provider", default="camoufox", choices=("camoufox", "playwright"))
    parser.add_argument("--parallel", type=int, default=2)
    parser.add_argument("--no-proxy", action="store_true")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--ensure-public", action="store_true")
    parser.add_argument("--convert-professional", action="store_true")
    parser.add_argument("--professional-type", choices=("creator", "business"), default="creator")
    parser.add_argument("--professional-category", default="Personal blog")
    parser.add_argument("--show-category", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    tasks = [item.strip() for item in args.tasks.split(",") if item.strip() in ALLOWED]
    if not tasks:
        from log_config import get_logger
        get_logger("onboarding").error("No valid onboarding tasks selected")
        print("[ERROR] No valid onboarding tasks selected", flush=True)
        return 2
    parallel = max(1, min(int(args.parallel or 3), 50))
    failed_steps = []
    for index, task in enumerate(tasks, start=1):
        from log_config import get_logger
        get_logger("onboarding").info(f"Account onboarding step {index}/{len(tasks)}: {task}")
        print(f"[OK] Account onboarding step {index}/{len(tasks)}: {task}", flush=True)
        if task == "create_profiles":
            command = [
                sys.executable, "-u", str(WORKFLOW), "--task", task,
                "--accounts", args.accounts, "--minutes", "8",
                "--provider", args.provider, "--max-workers", "1",
            ]
        else:
            effective_task = "auto_login_setup" if task == "auto_login" and (args.ensure_public or args.convert_professional) else task
            command = [
                sys.executable, "-u", str(SCHEDULER), "--operation", "workflow",
                "--task", effective_task, "--accounts", args.accounts,
                "--minutes", "8", "--provider", args.provider,
                "--parallel", str(parallel), "--arrive", "direct",
            ]
        if task == "auto_login" and (args.ensure_public or args.convert_professional):
            if args.ensure_public:
                command.append("--ensure-public")
            if args.convert_professional:
                command.append("--convert-professional")
            command += ["--professional-type", args.professional_type,
                        "--professional-category", args.professional_category]
            if args.show_category:
                command.append("--show-category")
        if args.no_proxy:
            command.append("--no-proxy")
        if args.headless:
            command.append("--headless")
        completed = subprocess.run(command, cwd=str(ROOT), check=False)
        if completed.returncode != 0:
            failed_steps.append(task)
            from log_config import get_logger
            get_logger("onboarding").warning(f"{task} completed with account errors (exit {completed.returncode}); continuing the onboarding chain")
            print(
                f"[WARNING] {task} completed with account errors (exit {completed.returncode}); continuing the onboarding chain",
                flush=True,
            )
    if failed_steps:
        from log_config import get_logger
        get_logger("onboarding").warning(f"Account onboarding completed with errors in: {', '.join(failed_steps)}")
        print(f"[WARNING] Account onboarding completed with errors in: {', '.join(failed_steps)}", flush=True)
        return 1
    from log_config import get_logger
    get_logger("onboarding").info(f"Account onboarding complete: {len(tasks)} step(s)")
    print(f"[OK] Account onboarding complete: {len(tasks)} step(s)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
