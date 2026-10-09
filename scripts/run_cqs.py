#!/usr/bin/env python3

"""Run the competency question SPARQL queries and check their results.

Each query's results are compared against a snapshot in expected/<CQ>.tsv.

Federated queries are mocked: each `SERVICE <endpoint>` clause is
rewritten to `GRAPH <endpoint>`, and a fixture file mocking that endpoint is
loaded into a named graph with the endpoint's IRI. Patterns inside the clause
therefore match only the mocked endpoint data, as they would remotely.

Run with --update to (re)write the snapshots from the current results, then
review the changes to expected/ before accepting them.

Exits with status 1 if any query fails, differs from its snapshot, has no
snapshot, or uses a SERVICE clause that cannot be mocked.
"""

import argparse
import csv
import re
import sys
from collections import Counter
from pathlib import Path

from rdflib import BNode, Dataset, Literal, URIRef

ROOT = Path(__file__).resolve().parent.parent

# Endpoint IRI -> fixture file mocking it
FIXTURES = {
    "https://dbpedia.org/sparql/": ROOT / "fixtures" / "dbpedia.ttl",
    "http://lod.xdams.org/sparql": ROOT / "fixtures" / "cdec.ttl",
}

SERVICE_KEYWORD = re.compile(r"\bSERVICE\b", re.IGNORECASE)
SERVICE_IRI = re.compile(r"\bSERVICE\s+(?:SILENT\s+)?<([^>]+)>", re.IGNORECASE)
ORDER_BY = re.compile(r"\bORDER\s+BY\b", re.IGNORECASE)
SELECT_STAR = re.compile(r"\bSELECT\s+(?:DISTINCT\s+|REDUCED\s+)?\*", re.IGNORECASE)


class CQFailure(Exception):
    pass


def load_dataset(data_paths):
    ds = Dataset()
    for path in data_paths:
        ds.default_graph.parse(path, format="turtle")
    for endpoint, path in FIXTURES.items():
        ds.graph(URIRef(endpoint)).parse(path, format="turtle")
    return ds


def mock_services(query):
    """Rewrite SERVICE clauses to GRAPH clauses over the fixture graphs."""
    endpoints = SERVICE_IRI.findall(query)
    if len(SERVICE_KEYWORD.findall(query)) != len(endpoints):
        raise CQFailure("SERVICE endpoint must be written as a full <IRI> to be mocked")
    missing = [e for e in endpoints if e not in FIXTURES]
    if missing:
        raise CQFailure(f"no fixture for endpoint(s) {', '.join(missing)}")
    return SERVICE_IRI.sub(r"GRAPH <\1>", query), endpoints


def is_ordered(query):
    """Whether the outermost query has ORDER BY (i.e. after the last '}')."""
    return bool(ORDER_BY.search(query[query.rfind("}") + 1:]))


def format_term(term):
    """An RDF term in Turtle syntax, as used by SPARQL TSV results."""
    if term is None:
        return ""
    if isinstance(term, URIRef):
        return f"<{term}>"
    if isinstance(term, BNode):
        return "_:b"  # blank node labels are not stable between runs
    if isinstance(term, Literal):
        escaped = (str(term).replace("\\", "\\\\").replace('"', '\\"')
                   .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
        if term.language:
            return f'"{escaped}"@{term.language}'
        if term.datatype:
            return f'"{escaped}"^^<{term.datatype}>'
        return f'"{escaped}"'
    raise TypeError(f"unexpected term {term!r}")


def run_query(ds, query):
    """Run a SELECT query; return its variable names and rows as tuples of terms."""
    query, endpoints = mock_services(query)
    try:
        result = ds.query(query)
    except Exception as e:
        raise CQFailure(f"{type(e).__name__}: {e}") from e
    if result.type != "SELECT":
        raise CQFailure(f"only SELECT queries are supported, not {result.type}")
    variables = result.vars
    if SELECT_STAR.search(query):
        # SELECT * gives no stable column order, so sort for reproducible snapshots
        variables = sorted(variables)
    rows = [tuple(format_term(row.get(v)) for v in variables) for row in result]
    variables = [str(v) for v in variables]
    return variables, rows, endpoints


def write_snapshot(path, variables, rows):
    lines = ["\t".join(f"?{v}" for v in variables)] + ["\t".join(row) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_snapshot(path):
    lines = path.read_text(encoding="utf-8").rstrip("\n").split("\n")
    variables = [v.lstrip("?") for v in lines[0].split("\t")]
    rows = [tuple(line.split("\t")) for line in lines[1:]]
    return variables, rows


def compare(expected_path, variables, rows, ordered):
    """Return a list of differences between a snapshot and actual results."""
    exp_vars, exp_rows = read_snapshot(expected_path)
    if set(exp_vars) != set(variables):
        return [f"variables differ: expected {exp_vars}, got {variables}"]
    # Reorder actual columns to the snapshot's column order
    order = [variables.index(v) for v in exp_vars]
    rows = [tuple(row[i] for i in order) for row in rows]

    if ordered:
        if rows == exp_rows:
            return []
        if Counter(rows) == Counter(exp_rows):
            return ["rows match but are in a different order"]
    missing = Counter(exp_rows) - Counter(rows)
    unexpected = Counter(rows) - Counter(exp_rows)
    return ([f"missing:    {' | '.join(r)}" for r in missing.elements()] +
            [f"unexpected: {' | '.join(r)}" for r in unexpected.elements()])


def read_cqs(path):
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            query = (row.get("SPARQL") or "").strip()
            if query:
                yield row["#"], query


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("data", nargs="*", type=Path, default=[ROOT / "example.ttl"],
                        help="Turtle file(s) to query (default: example.ttl)")
    parser.add_argument("--cqs", type=Path, default=ROOT / "competency_questions.csv",
                        help="competency questions CSV (default: competency_questions.csv)")
    parser.add_argument("--expected", type=Path, default=ROOT / "expected",
                        help="directory of result snapshots (default: expected/)")
    parser.add_argument("--update", action="store_true",
                        help="write snapshots from the current results instead of checking them")
    parser.add_argument("-v", "--verbose", action="store_true", help="print result rows")
    args = parser.parse_args()

    ds = load_dataset(args.data)
    args.expected.mkdir(exist_ok=True)
    failures = 0
    seen = set()
    for cq_id, query in read_cqs(args.cqs):
        snapshot = args.expected / f"{cq_id}.tsv"
        seen.add(snapshot.name)
        try:
            variables, rows, endpoints = run_query(ds, query)
        except CQFailure as e:
            print(f"FAIL  {cq_id}: {e}")
            failures += 1
            continue

        mocked = f" (mocked: {', '.join(endpoints)})" if endpoints else ""
        if args.update:
            write_snapshot(snapshot, variables, rows)
            print(f"wrote {cq_id}: {len(rows)} row(s){mocked}")
        elif not snapshot.exists():
            print(f"FAIL  {cq_id}: no snapshot {snapshot.relative_to(ROOT)} (run with --update)")
            failures += 1
        else:
            diffs = compare(snapshot, variables, rows, is_ordered(query))
            print(f"{'FAIL' if diffs else 'ok  '}  {cq_id}: {len(rows)} row(s){mocked}")
            for diff in diffs:
                print(f"        {diff}")
            failures += bool(diffs)
        if args.verbose:
            for row in rows:
                print("        " + " | ".join(row))

    for stale in sorted(p.name for p in args.expected.glob("*.tsv") if p.name not in seen):
        print(f"warning: snapshot {stale} has no matching competency question")

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
