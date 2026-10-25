"""``culture-library learn`` — the learnability affordance.

Prints a structured self-teaching prompt. Must satisfy the agent-first rubric:
>=200 chars and mention purpose, command map, exit codes, --json, and explain.
"""

from __future__ import annotations

import argparse

from culture_library import __version__
from culture_library.cli._output import emit_result

_TEXT = """\
culture-library — a clonable template for AgentCulture mesh agents.

Purpose
-------
Scaffold for a new Culture mesh agent: an agent-first CLI (cited from the teken
`python-cli` reference), an identity (culture.yaml + CLAUDE.md), the canonical
guildmaster skill kit under .claude/skills/, and a deploy/CI baseline. Clone it,
rename the package, and edit culture.yaml to mint a new agent.

Commands
--------
  culture-library whoami             Identity from culture.yaml.
  culture-library learn              This self-teaching prompt.
  culture-library explain <path>...  Markdown docs for any noun/verb path.
  culture-library overview           Descriptive snapshot of the agent.
  culture-library doctor             Check the agent-identity invariants.
  culture-library cli overview       Describe the CLI surface itself.
  culture-library server serve       Start the HTTP server (needs culture-library[server]).
  culture-library server overview    Describe the server noun.
  culture-library migrate from-xteink  Copy an xteink library in (dry run unless --apply).
  culture-library migrate overview   Describe the migrate noun.
  culture-library keys mint          Mint an API key (shown once).
  culture-library keys overview      Describe the keys noun.
  culture-library backup run         Snapshot DB + files to a second location.
  culture-library backup restore     Restore a snapshot into an empty dir (verified).
  culture-library backup overview    Describe the backup noun.
  culture-library deploy restart     Recreate library + mcp from the host env file.
  culture-library deploy overview    Describe the deploy noun.

Machine-readable output
-----------------------
Every command supports --json. Errors in JSON mode emit
{"code", "message", "remediation"} to stderr. Stdout and stderr never mix.

Exit-code policy
----------------
  0 success
  1 user-input error (bad flag, bad path, missing arg)
  2 environment / setup error
  3+ reserved

More detail
-----------
  culture-library explain culture-library
"""


def _as_json_payload() -> dict[str, object]:
    return {
        "tool": "culture-library",
        "version": __version__,
        "purpose": "Clonable scaffold for a new AgentCulture mesh agent.",
        "commands": [
            {"path": ["whoami"], "summary": "Identity probe from culture.yaml."},
            {"path": ["learn"], "summary": "Self-teaching prompt."},
            {"path": ["explain"], "summary": "Markdown docs by path."},
            {"path": ["overview"], "summary": "Descriptive snapshot of the agent."},
            {"path": ["doctor"], "summary": "Check the agent-identity invariants."},
            {"path": ["cli", "overview"], "summary": "Describe the CLI surface."},
            {"path": ["server", "serve"], "summary": "Start the HTTP server ([server] extra)."},
            {"path": ["server", "overview"], "summary": "Describe the server noun."},
            {"path": ["migrate", "from-xteink"], "summary": "Migrate xteink items (--apply)."},
            {"path": ["migrate", "overview"], "summary": "Describe the migrate noun."},
            {"path": ["keys", "mint"], "summary": "Mint an API key (shown once)."},
            {"path": ["keys", "overview"], "summary": "Describe the keys noun."},
            {"path": ["backup", "run"], "summary": "Snapshot DB + files to a second location."},
            {"path": ["backup", "restore"], "summary": "Restore a snapshot (verified)."},
            {"path": ["backup", "overview"], "summary": "Describe the backup noun."},
            {"path": ["deploy", "restart"], "summary": "Recreate library + mcp (env file)."},
            {"path": ["deploy", "overview"], "summary": "Describe the deploy noun."},
        ],
        "exit_codes": {
            "0": "success",
            "1": "user-input error",
            "2": "environment/setup error",
        },
        "json_support": True,
        "explain_pointer": "culture-library explain <path>",
    }


def cmd_learn(args: argparse.Namespace) -> int:
    if getattr(args, "json", False):
        emit_result(_as_json_payload(), json_mode=True)
    else:
        emit_result(_TEXT, json_mode=False)
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "learn",
        help="Print a structured self-teaching prompt for agent consumers.",
    )
    p.add_argument("--json", action="store_true", help="Emit structured JSON.")
    p.set_defaults(func=cmd_learn)
