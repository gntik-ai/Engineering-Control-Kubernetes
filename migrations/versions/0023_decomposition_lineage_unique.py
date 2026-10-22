"""One active change request per backlog decomposition lineage (NEW-234 PR6 review).

Revision ID: 0023
Revises: 0022

A partial unique index on change_requests.decomposition_lineage over the active statuses
(DRAFTING, CANDIDATE, APPROVED, PUBLISHED): two approved decompositions with different plan
hashes fan out under different request ids, and only this constraint makes the lineage
dedupe (check, then insert) race-proof between them. A rejected, failed or merged request
does not hold its lineage.

Locking: as in 0022, the index builds in this revision's transaction (ACCESS EXCLUSIVE on
change_requests); the deploy stops every unit that writes the table before it migrates,
the table is small, and lock acquisition and statements are bounded (lock_timeout 5s,
statement_timeout 30s) so a held lock fails the migration closed. An existing index (the
0001 bootstrap of the current models) is kept only when its columns, uniqueness and
predicate match exactly; duplicates refuse the upgrade; the downgrade refuses while any
active decomposition request exists (the running code relies on the constraint).
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "uq_change_requests_active_decomposition_lineage"
ACTIVE_STATUSES = ("DRAFTING", "CANDIDATE", "APPROVED", "PUBLISHED")
ACTIVE_PREDICATE = (
    "decomposition_lineage IS NOT NULL AND "
    "status IN ('DRAFTING', 'CANDIDATE', 'APPROVED', 'PUBLISHED')"
)


def _bound() -> None:
    dialect = op.get_bind().dialect.name
    if dialect not in {"postgresql", "sqlite"}:
        raise RuntimeError("revision 0023 supports PostgreSQL and SQLite only")
    if dialect == "postgresql":
        op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
        op.execute(sa.text("SET LOCAL statement_timeout = '30s'"))


def _reflected_predicate() -> tuple[str | None, bool]:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        row = bind.execute(
            sa.text(
                "SELECT pg_get_expr(index.indpred, index.indrelid), index.indisvalid "
                "FROM pg_class AS index_relation "
                "JOIN pg_index AS index ON index.indexrelid = index_relation.oid "
                "JOIN pg_class AS table_relation ON table_relation.oid = index.indrelid "
                "JOIN pg_namespace AS namespace ON namespace.oid = table_relation.relnamespace "
                "WHERE namespace.nspname = current_schema() "
                "AND table_relation.relname = 'change_requests' "
                "AND index_relation.relkind = 'i' "
                "AND index_relation.relname = :index_name"
            ),
            {"index_name": INDEX_NAME},
        ).first()
        if row is None:
            return None, False
        return str(row[0]) if row[0] is not None else None, bool(row[1])
    definition = bind.scalar(
        sa.text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :index_name"),
        {"index_name": INDEX_NAME},
    )
    if not isinstance(definition, str) or " WHERE " not in definition.upper():
        return None, False
    return re.split(r"\bwhere\b", definition, maxsplit=1, flags=re.IGNORECASE)[-1], True


_CAST = re.compile(r"::(?:character varying|varchar|text)(?:\(\d+\))?(?:\[\])?")
_LITERAL = re.compile(r"'(?:[^']|'')*'")
_EXPECTED = re.compile(r"^decomposition_lineage is not null and status in \$(?:, \$)*$")


def _normalized_predicate(value: str) -> tuple[str, list[str]]:
    """The predicate as PostgreSQL (pg_get_expr) or SQLite (sqlite_master) renders it, with
    every single-quoted literal replaced by `$` and returned byte-exact (`''` unescaped).
    Only the text outside literals is case-folded and stripped of quoting, casts,
    parentheses and brackets, with `= ANY (ARRAY[...])` read as `in`:
    `decomposition_lineage is not null and status in $, $, $, $`."""
    literals: list[str] = []
    pieces: list[str] = []
    position = 0
    for match in _LITERAL.finditer(value):
        pieces.append(value[position : match.start()].lower())
        pieces.append(" $ ")
        literals.append(match.group(0)[1:-1].replace("''", "'"))
        position = match.end()
    pieces.append(value[position:].lower())
    text = _CAST.sub("", "".join(pieces)).replace('"', "").replace("change_requests.", "")
    text = re.sub(r"[()\[\]]", " ", text)
    text = re.sub(r"=\s*any\s+array", " in ", text)
    text = re.sub(r"\s*,\s*", ", ", text)
    return re.sub(r"\s+", " ", text).strip().rstrip(";").strip(), literals


def _predicate_is_compatible(value: str | None) -> bool:
    """Exactly the expected predicate: the lineage is set and the status is one of the
    four active ones, byte-exact (in any order, each once); any other conjunct, disjunct,
    negation, constant (`AND revision > 1`, `AND FALSE`) or literal ('drafting') is
    refused."""
    if value is None or "'" in _LITERAL.sub("", value):
        return False
    text, literals = _normalized_predicate(value)
    if _EXPECTED.fullmatch(text) is None:
        return False
    return len(literals) == len(ACTIVE_STATUSES) and set(literals) == set(ACTIVE_STATUSES)


def _existing_index_is_compatible() -> bool:
    indexes = {
        str(index["name"]): index
        for index in sa.inspect(op.get_bind()).get_indexes("change_requests")
        if index.get("name")
    }
    existing = indexes.get(INDEX_NAME)
    if existing is None:
        return False
    if not bool(existing.get("unique")) or list(existing.get("column_names") or []) != [
        "decomposition_lineage"
    ]:
        raise RuntimeError("revision 0023 found an incompatible decomposition lineage index")
    predicate, valid = _reflected_predicate()
    if not valid or not _predicate_is_compatible(predicate):
        raise RuntimeError("revision 0023 found an incompatible decomposition lineage predicate")
    return True


def upgrade() -> None:
    _bound()
    if _existing_index_is_compatible():
        return
    duplicate = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT decomposition_lineage, count(*) AS active_count FROM change_requests "
                "WHERE decomposition_lineage IS NOT NULL AND "
                "status IN ('DRAFTING', 'CANDIDATE', 'APPROVED', 'PUBLISHED') "
                "GROUP BY decomposition_lineage HAVING count(*) > 1 LIMIT 1"
            )
        )
        .first()
    )
    if duplicate is not None:
        raise RuntimeError(
            "revision 0023 refuses duplicate active decomposition requests; reject all but "
            f"one of lineage={str(duplicate.decomposition_lineage)[:160]!r} "
            f"active_count={int(duplicate.active_count)}"
        )
    predicate = sa.text(ACTIVE_PREDICATE)
    op.create_index(
        INDEX_NAME,
        "change_requests",
        ["decomposition_lineage"],
        unique=True,
        postgresql_where=predicate,
        sqlite_where=predicate,
    )


def downgrade() -> None:
    _bound()
    active = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT COUNT(*) FROM change_requests WHERE decomposition_lineage IS NOT NULL "
                "AND status IN ('DRAFTING', 'CANDIDATE', 'APPROVED', 'PUBLISHED')"
            )
        )
        .scalar()
    )
    if active:
        raise RuntimeError(
            "revision 0023 downgrade refuses active decomposition change requests "
            "(restore a backup instead)"
        )
    if any(
        index.get("name") == INDEX_NAME
        for index in sa.inspect(op.get_bind()).get_indexes("change_requests")
    ):
        op.drop_index(INDEX_NAME, table_name="change_requests")
