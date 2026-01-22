import argparse
from datetime import datetime
import sys

from devtools.common import require_dev_environment


def _set_state(user_id: int, state: str) -> None:
    from app.db import SessionLocal
    from app.fsm.states import FSM_ALLOWED_STATES

    if state not in FSM_ALLOWED_STATES:
        allowed = ", ".join(sorted(FSM_ALLOWED_STATES))
        raise ValueError(f"state '{state}' is not allowed. Allowed states: {allowed}")

    with SessionLocal() as db:
        from app.db import User

        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise ValueError(f"user_id {user_id} not found")
        previous = user.current_state
        user.current_state = state
        db.commit()

    print(f"[FSM] user {user_id}: {previous} -> {state}")


def _inspect(user_id: int) -> None:
    from app.db import SessionLocal
    from devtools.common import load_active_plan, load_user

    with SessionLocal() as db:
        user = load_user(db, user_id)
        if not user:
            raise ValueError(f"user_id {user_id} not found")
        active_plan = load_active_plan(db, user_id)

    plan_id = active_plan.id if active_plan else None
    plan_status = active_plan.status if active_plan else None
    adaptation_version = active_plan.adaptation_version if active_plan else None
    plan_end_date = user.plan_end_date.isoformat() if user.plan_end_date else None

    print("FSM State Inspection")
    print(f"  user_id: {user_id}")
    print(f"  current_state: {user.current_state}")
    print(f"  active_plan_id: {plan_id}")
    print(f"  plan_status: {plan_status}")
    print(f"  adaptation_version: {adaptation_version}")
    print(f"  plan_end_date: {plan_end_date}")
    print(f"  inspected_at: {datetime.utcnow().isoformat()}Z")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FSM devtools")
    subparsers = parser.add_subparsers(dest="command", required=True)

    set_state = subparsers.add_parser("set-state", help="Force-set FSM state")
    set_state.add_argument("--user-id", type=int, required=True)
    set_state.add_argument("--state", required=True)

    inspect = subparsers.add_parser("inspect", help="Inspect current FSM state")
    inspect.add_argument("--user-id", type=int, required=True)

    return parser


def main() -> None:
    try:
        require_dev_environment()
        parser = build_parser()
        args = parser.parse_args()

        if args.command == "set-state":
            _set_state(args.user_id, args.state)
        elif args.command == "inspect":
            _inspect(args.user_id)
        else:
            parser.error("Unknown command")
    except Exception as exc:  # noqa: BLE001 - CLI entrypoint
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
