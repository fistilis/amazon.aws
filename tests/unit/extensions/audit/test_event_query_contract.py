# -*- coding: utf-8 -*-

# Copyright: Contributors to the Ansible project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

"""Contract tests for ``extensions/audit/event_query.yml``.

The jq queries in that file do not just have to be valid jq -- their *output* has to satisfy a
contract imposed by the consumer, ansible-automation-platform's indirect node counting. Output that
violates the contract is silently discarded or mis-bucketed at ingest; the collection itself sees no
error, and neither does any existing integration test, because those assert against hand-written
literals rather than against the contract.

Three requirements, checked here:

1. A non-null top-level ``name``. The consumer cannot persist a record without one and drops it.
2. ``infra_type``, ``infra_bucket`` and ``device_type`` are all emitted. Without them a node is
   counted but cannot be bucketed, so it disappears from every rollup.
3. Taxonomy values are normalized ``lowercase_with_underscores``. ``PublicCloud`` and
   ``public_cloud`` become two distinct buckets downstream.

These are static checks by design: they need no AWS credentials and no live resources, so they run
in the units job on every pull request -- including pull requests that touch only
``extensions/audit/``, which the integration test splitter does not select targets for.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

# tests/unit/extensions/audit/<this file> -> collection root
COLLECTION_ROOT = Path(__file__).resolve().parents[4]
EVENT_QUERY_PATH = COLLECTION_ROOT / "extensions" / "audit" / "event_query.yml"

TAXONOMY_KEYS = ("infra_type", "infra_bucket", "device_type")

# The normalized form the consumer's taxonomy expects: lowercase alphanumerics, underscore separated.
NORMALIZED = re.compile(r"^[a-z0-9]+(_[a-z0-9]+)*$")


def load_queries():
    """Return ``{module_name: jq_query}`` for every entry in event_query.yml."""
    with EVENT_QUERY_PATH.open(encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}
    return {name: spec["query"] for name, spec in document.items() if isinstance(spec, dict) and "query" in spec}


QUERIES = load_queries()


def brace_blocks(query):
    """Yield ``(start, end, inner_text)`` for every balanced ``{...}`` block in ``query``."""
    stack = []
    for index, character in enumerate(query):
        if character == "{":
            stack.append(index)
        elif character == "}" and stack:
            start = stack.pop()
            yield start, index, query[start + 1 : index]


def emitted_object(query):
    """Return the inner text of the object the query emits.

    A query may build helper objects (lookup tables keyed by resource type, for example) before
    emitting its result, so the first ``{`` is not reliably the emitted object. The emitted object is
    the outermost block that contains ``canonical_facts``; fall back to the outermost block overall.
    """
    blocks = list(brace_blocks(query))
    if not blocks:
        return ""
    with_canonical_facts = [block for block in blocks if "canonical_facts" in block[2]]
    candidates = with_canonical_facts or blocks
    return max(candidates, key=lambda block: block[1] - block[0])[2]


def top_level_keys(query):
    """Return the keys declared at the top level of the emitted object."""
    body = emitted_object(query)
    depth = 0
    top_level = ""
    for character in body:
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
        elif depth == 0:
            top_level += character
    return re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:", top_level)


def taxonomy_literals(query, key):
    """Return the string literals ``key`` can be assigned in the emitted object.

    A literal assignment yields one value. A computed assignment -- typically a lookup into a mapping
    object defined earlier in the query -- yields every literal that lookup can resolve to, so
    table-driven queries are still checked against their own tables. Returns ``None`` when the key is
    not emitted at all.
    """
    body = emitted_object(query)
    match = re.search(rf'{key}\s*:\s*("([^"]*)"|\(([^)]*)\)|([^,\n}}]+))', body)
    if not match:
        return None
    if match.group(2) is not None:
        return [match.group(2)]
    expression = (match.group(3) or match.group(4) or "").strip()
    literals = re.findall(r'"([^"]*)"', expression)
    if "mapping" in expression:
        # Resolve through the lookup table the expression indexes into.
        literals += re.findall(r':\s*"([^"]*)"', query)
    return literals


def test_event_query_file_is_present_and_parses():
    assert EVENT_QUERY_PATH.is_file(), f"{EVENT_QUERY_PATH} is missing"
    assert QUERIES, "event_query.yml declares no module queries"


@pytest.mark.parametrize("module_name", sorted(QUERIES))
def test_query_emits_top_level_name(module_name):
    """Records without a top-level ``name`` are discarded by the consumer."""
    keys = top_level_keys(QUERIES[module_name])
    assert "name" in keys, (
        f"{module_name}: the emitted object has no top-level 'name'. Records without one are dropped "
        f"at ingest, so this module contributes no node data. Emitted top-level keys: {sorted(set(keys))}"
    )


@pytest.mark.parametrize("module_name", sorted(QUERIES))
def test_query_emits_full_taxonomy(module_name):
    """All three taxonomy keys are required to bucket a node."""
    query = QUERIES[module_name]
    missing = [key for key in TAXONOMY_KEYS if taxonomy_literals(query, key) is None]
    assert not missing, (
        f"{module_name}: taxonomy keys {missing} are not emitted. Nodes from this module are counted "
        f"but cannot be bucketed, so they are absent from every rollup."
    )


@pytest.mark.parametrize("module_name", sorted(QUERIES))
def test_taxonomy_values_are_normalized(module_name):
    """Taxonomy values must be lowercase_with_underscores.

    This is a shape assertion rather than an equality assertion on purpose. Equality against a
    hand-written expected value only ever describes the resource types someone wrote a fixture for,
    so it cannot catch an unmapped resource type falling through to a raw fallback.
    """
    query = QUERIES[module_name]
    offenders = {}
    for key in TAXONOMY_KEYS:
        literals = taxonomy_literals(query, key) or []
        bad = sorted({value for value in literals if value and not NORMALIZED.match(value)})
        if bad:
            offenders[key] = bad
    assert not offenders, (
        f"{module_name}: taxonomy values are not normalized: {offenders}. The consumer treats "
        f"'PublicCloud' and 'public_cloud' as two different buckets, which splits the node count."
    )


@pytest.mark.parametrize("module_name", sorted(QUERIES))
def test_query_compiles_as_jq(module_name):
    """Guard against a query that is syntactically invalid jq.

    Skipped where the jq bindings are unavailable; they are an integration test dependency, not a
    unit test one, so this check is opportunistic.
    """
    jq = pytest.importorskip("jq", reason="jq bindings not installed")
    jq.compile(QUERIES[module_name])
