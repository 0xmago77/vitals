"""Command line: `python -m vitals <command>`.

    serve                                  run the HTTP server and workers
    report ADDRESS [--block N] [--target T]  print the health-factor report
    keeper open --collateral-bnb X [--target-hf 2.0]
    keeper cycle                           run one keeper decision now
    keeper status                          print the keeper status
    jobs tick                              run one ERC-8183 watcher iteration
    card | registration                    print the agent card / ERC-8004 file

Writes are dry runs unless VITALS_LIVE=1.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal


def _dump(obj) -> None:
    print(json.dumps(obj, indent=2, default=lambda o: float(o) if isinstance(o, Decimal) else str(o)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vitals")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    rp = sub.add_parser("report")
    rp.add_argument("address")
    rp.add_argument("--block", type=int)
    rp.add_argument("--target", default="2.0")
    kp = sub.add_parser("keeper")
    ks = kp.add_subparsers(dest="kcmd", required=True)
    ko = ks.add_parser("open")
    ko.add_argument("--collateral-bnb", required=True)
    ko.add_argument("--target-hf", default=None)
    ks.add_parser("cycle")
    ks.add_parser("status")
    jp = sub.add_parser("jobs")
    js = jp.add_subparsers(dest="jcmd", required=True)
    js.add_parser("tick")
    js.add_parser("list")
    sub.add_parser("card")
    sub.add_parser("registration")
    args = ap.parse_args(argv)

    from .config import Config

    cfg = Config.from_env()
    if args.cmd == "serve":
        from .server import serve

        serve(cfg)
        return 0
    if args.cmd == "card":
        from .card import agent_card

        _dump(agent_card(cfg))
        return 0
    if args.cmd == "registration":
        from .card import registration_file

        _dump(registration_file(cfg))
        return 0

    from .rpc import pool_from_config

    pool = pool_from_config(cfg)
    if args.cmd == "report":
        from .service import HFService

        ok, payload = HFService(cfg, pool).answer(None, {"address": args.address, "blockNumber": args.block,
                                                         "targetHealthFactor": args.target})
        _dump(payload)
        return 0 if ok else 1

    from .chain import WriteRefused, load_account
    from .db import DB
    from .rpc import RpcPool

    db = DB(cfg.db_path)
    account = load_account(cfg)
    write_pool = RpcPool(cfg.rpc_write or cfg.rpc_head, cfg.rpc_archive, logs=cfg.rpc_logs, timeout=cfg.rpc_timeout)
    if args.cmd == "keeper":
        from .keeper import Keeper

        k = Keeper(cfg, pool, db, account, write_pool=write_pool)
        try:
            if args.kcmd == "open":
                _dump(k.open_position(Decimal(args.collateral_bnb),
                                      Decimal(args.target_hf) if args.target_hf else None))
            elif args.kcmd == "cycle":
                _dump(k.cycle())
            else:
                _dump(k.status())
        except WriteRefused as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        return 0
    if args.cmd == "jobs":
        from .seller import Seller
        from .service import HFService

        s = Seller(cfg, pool, db, HFService(cfg, pool), account, write_pool=write_pool)
        _dump(s.tick() if args.jcmd == "tick" else s.public_jobs())
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
