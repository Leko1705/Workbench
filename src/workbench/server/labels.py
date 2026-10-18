"""Labels and the communication rules between them.

A label is a cluster of workers (e.g. a role). Every worker carries at least one label;
a worker that joins gets a fresh label of its own. Who may start tasks on whom is
defined between labels:

  policy "open"        every label may talk to every label (the default)
  policy "restricted"  only the listed (from, to) pairs may; a rule is directed, so
                       A -> B lets agents labelled A hand work to agents labelled B

A worker with several labels may act through any of them. Hosts and API keys are not
bound by the rules; they act from outside the network.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from .store import Record, WorkbenchStore

OPEN, RESTRICTED = "open", "restricted"
DEFAULT_COMMUNICATION: dict[str, Any] = {"policy": OPEN, "rules": []}


async def communication(store: WorkbenchStore) -> dict[str, Any]:
    c = (await store.get_settings()).get("communication") or DEFAULT_COMMUNICATION
    return {"policy": c.get("policy", OPEN), "rules": [tuple(r) for r in c.get("rules", [])]}


def may_communicate(comm: dict[str, Any], from_labels: list[str], to_labels: list[str]) -> bool:
    if comm["policy"] == OPEN:
        return True
    rules = set(comm["rules"])
    return any((a, b) in rules for a in from_labels for b in to_labels)


async def create_label(store: WorkbenchStore, wanted: str) -> Record:
    """A new label named `wanted` (made unique by a numeric suffix if needed)."""
    taken = {l["name"].lower() for l in await store.list_labels()}
    name, n = wanted.strip()[:100] or "agent", 1
    while name.lower() in taken:
        n += 1
        name = f"{wanted.strip()[:94]} {n}"
    label = {"id": uuid.uuid4().hex, "name": name, "created_at": time.time()}
    await store.add_label(label)
    return label
