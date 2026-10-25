"""``culture-library keys`` — mint, list and revoke API keys on this host.

The HTTP ``/api/keys`` routes need an admin key, so the first admin key has to come from
here. Keys are shown once at mint and stored only as a hash.
"""

from __future__ import annotations

import argparse
import os

from culture_library.cli._commands.overview import emit_overview
from culture_library.cli._errors import EXIT_USER_ERROR, CliError
from culture_library.cli._output import emit_diagnostic, emit_result
from culture_library.core.store import DATA_DIR_ENV

_JSON_HELP = "Emit structured JSON."


def _json(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False))


def _store(args: argparse.Namespace):
    from culture_library.core import Store

    return Store(args.data_dir or os.environ.get(DATA_DIR_ENV) or None)


def cmd_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "culture-library keys",
        [
            {
                "title": "Verbs",
                "items": [
                    "mint --name N --scope read|write|admin [--kind K] [--private] — "
                    "create a key and print it once",
                    "list — keys with scopes, kinds and status (never the secret)",
                    "revoke --id N — revoke a key",
                    "rotate ID [--kind K] [--drop-kind K] [--scope S] [--private|--no-private] "
                    "— mint a replacement with ID's settings (old key stays valid)",
                    "rotate ID --confirm NEW_ID — revoke ID once NEW_ID, its replacement, is in "
                    "use",
                    "overview — this description",
                ],
            },
            {
                "title": "Scopes",
                "items": [
                    "read: read the catalog; write implies read; admin implies both and "
                    "manages keys",
                    "--kind limits writes to those kinds; --private lets the key see "
                    "private items (admin keys always can)",
                ],
            },
        ],
        json_mode=_json(args),
    )
    return 0


def cmd_mint(args: argparse.Namespace) -> int:
    from culture_library.core import keys
    from culture_library.core.errors import ValidationError

    try:
        key, raw = keys.mint(
            _store(args), args.name, args.scope, kinds=args.kind or None, private=args.private
        )
    except ValidationError as exc:
        raise CliError(EXIT_USER_ERROR, str(exc), "see 'culture-library keys overview'") from None
    if _json(args):
        emit_result({"api_key": key.to_dict(), "key": raw}, json_mode=True)
    else:
        emit_result(raw, json_mode=False)  # stdout carries only the key, ready to pipe
        emit_diagnostic(
            f"(key {key.key_id}, scopes {','.join(key.scopes)}; shown once, store it in "
            "a secret manager)"
        )
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    from culture_library.core import keys

    rows = [k.to_dict() for k in keys.list_keys(_store(args))]
    if _json(args):
        emit_result({"keys": rows}, json_mode=True)
    else:
        lines = [
            f"{r['id']}\t{r['key_id']}\t{r['name']}\t{','.join(r['scopes'])}\t"
            f"{','.join(r['kinds']) if r['kinds'] is not None else 'any kind'}\t"
            f"{'private' if r['private'] else '-'}\t{'revoked' if r['revoked_at'] else 'active'}"
            + (f"\treplaces {r['replaces']}" if r["replaces"] is not None else "")
            for r in rows
        ]
        emit_result("\n".join(lines) or "no keys", json_mode=False)
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    from culture_library.core import keys
    from culture_library.core.errors import NotFoundError

    try:
        key = keys.revoke(_store(args), args.id)
    except NotFoundError:
        raise CliError(
            EXIT_USER_ERROR, f"no key with id {args.id}", "run 'culture-library keys list'"
        ) from None
    emit_result(
        {"api_key": key.to_dict()} if _json(args) else f"revoked {key.key_id}",
        json_mode=_json(args),
    )
    return 0


def _rotation(old_id: int, new) -> dict:
    """The rotate/confirm report (the new key's settings); never the secret."""
    return {
        "old_id": old_id,
        "new_id": new.id,
        "name": new.name,
        "scope": list(new.scopes),
        "kinds": list(new.kinds) if new.kinds is not None else None,
        "private": new.private,
    }


def cmd_rotate(args: argparse.Namespace) -> int:
    from culture_library.core import keys
    from culture_library.core.errors import NotFoundError, ValidationError

    store = _store(args)
    if args.confirm is not None:
        if args.kind or args.drop_kind or args.scope or args.private is not None:
            raise CliError(
                EXIT_USER_ERROR,
                "--confirm takes no key settings (they were fixed when the key was minted)",
                f"run 'culture-library keys rotate {args.id} --confirm {args.confirm}' alone",
            )
        try:
            old, new = keys.confirm_rotation(store, args.id, args.confirm)
        except NotFoundError as exc:
            raise CliError(EXIT_USER_ERROR, str(exc), "run 'culture-library keys list'") from None
        except keys.RotationError as exc:
            raise CliError(
                EXIT_USER_ERROR,
                f"{exc.code}: {exc}; nothing was changed",
                "pass the new id printed by 'keys rotate ID'; see 'culture-library keys list'",
            ) from None
        emit_result(
            (
                _rotation(old.id, new)
                if _json(args)
                else f"revoked key {old.id}; key {new.id} is live"
            ),
            json_mode=_json(args),
        )
        return 0
    try:
        new, raw = keys.rotate(
            store,
            args.id,
            add_kinds=args.kind or (),
            drop_kinds=args.drop_kind or (),
            scopes=args.scope,
            private=args.private,
        )
    except NotFoundError as exc:
        raise CliError(EXIT_USER_ERROR, str(exc), "run 'culture-library keys list'") from None
    except keys.RotationError as exc:
        raise CliError(
            EXIT_USER_ERROR, f"{exc.code}: {exc}", "rotate a live key; see 'keys list'"
        ) from None
    except ValidationError as exc:
        raise CliError(EXIT_USER_ERROR, str(exc), "see 'culture-library keys overview'") from None
    if _json(args):
        emit_result({**_rotation(args.id, new), "key": raw}, json_mode=True)
    else:
        emit_result(raw, json_mode=False)  # stdout carries only the key, ready to pipe
        emit_diagnostic(
            f"(key {new.id} replaces key {args.id}; shown once. Key {args.id} stays valid: once "
            f"the consumer uses the new key, run 'culture-library keys rotate {args.id} "
            f"--confirm {new.id}')"
        )
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("keys", help="API keys (see 'culture-library keys overview').")
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=cmd_overview, json=False)
    nsub = p.add_subparsers(dest="keys_command", parser_class=type(p))

    def common(q: argparse.ArgumentParser) -> None:
        q.add_argument("--data-dir", help=f"Library data dir (default ${DATA_DIR_ENV}).")
        q.add_argument("--json", action="store_true", help=_JSON_HELP)

    m = nsub.add_parser("mint", help="Create a key and print it once.")
    m.add_argument("--name", required=True, help="Who or what the key is for.")
    m.add_argument("--scope", action="append", required=True, choices=("read", "write", "admin"))
    m.add_argument("--kind", action="append", help="Limit writes to this kind (repeatable).")
    m.add_argument("--private", action="store_true", help="Let the key see private items.")
    common(m)
    m.set_defaults(func=cmd_mint)
    ls = nsub.add_parser("list", help="List keys (never the secret).")
    common(ls)
    ls.set_defaults(func=cmd_list)
    rv = nsub.add_parser("revoke", help="Revoke a key.")
    rv.add_argument("--id", type=int, required=True, help="Key id from 'keys list'.")
    common(rv)
    rv.set_defaults(func=cmd_revoke)
    rt = nsub.add_parser("rotate", help="Replace a key: mint with its settings, then --confirm.")
    rt.add_argument("id", type=int, help="Id of the key to replace (from 'keys list').")
    rt.add_argument("--kind", action="append", help="Add a kind to the copied list (repeatable).")
    rt.add_argument("--drop-kind", action="append", help="Remove a kind (repeatable).")
    rt.add_argument(
        "--scope", action="append", choices=("read", "write", "admin"), help="Replace scopes."
    )
    priv = rt.add_mutually_exclusive_group()
    priv.add_argument("--private", dest="private", action="store_true", default=None)
    priv.add_argument("--no-private", dest="private", action="store_false")
    rt.add_argument("--confirm", type=int, metavar="NEW_ID", help="Revoke ID; NEW_ID replaced it.")
    common(rt)
    rt.set_defaults(func=cmd_rotate)
    ov = nsub.add_parser("overview", help="Describe the keys noun.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_overview)
