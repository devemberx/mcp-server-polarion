"""REST-SQL ``query`` string builders for the ``tools`` package (not public API).

Polarion's REST ``query`` param accepts a ``SQL:(...)`` prefix that unlocks
joins and aggregates Lucene cannot express. REST SQL has no bind parameters, so
every id is escaped inline by doubling ``'`` (matching ``list_work_items``). A
``SELECT`` over the ``workitem`` table always yields work-item resources — the
column list is ignored — so callers pair these with ``include=``/``fields=`` to
read the attributes they actually need.
"""

from __future__ import annotations


def one_heading_per_document_sql(project_id: str) -> str:
    """``GROUP BY`` SQL returning one representative heading per document.

    Groups every heading on its ``module`` URI and returns the ``MIN`` work-item
    URI per group, collapsing all headings to one row per document. REST SQL
    returns work-item resources (the ``SELECT`` column list is ignored), so the
    caller pairs this with ``include=module`` to read each document's attributes.
    ``c_deleted`` is boolean -- ``IS NOT TRUE`` excludes recycle-bin documents
    to match the Lucene ``type:heading`` default (``= 0`` 500s).
    """
    project = project_id.replace("'", "''")
    return (
        "SQL:(SELECT MIN(wi.c_uri) FROM workitem wi "  # noqa: S608
        "INNER JOIN project p ON wi.fk_uri_project = p.c_uri "
        "INNER JOIN module mod ON wi.fk_uri_module = mod.c_uri "
        f"WHERE p.c_id = '{project}' AND wi.c_type = 'heading' "
        "AND wi.c_deleted IS NOT TRUE GROUP BY mod.c_uri)"
    )


def one_item_per_custom_field_sql(project_id: str, type_id: str) -> str:
    """``GROUP BY`` SQL returning one representative item per custom-field key.

    ``GROUP BY cf.c_name`` + ``MIN`` = one item per distinct key, so the union
    catches single-item keys a fixed-N sample misses. Groups on indexed
    ``c_name`` — far lighter than per-item ``COUNT(...) OVER ()``.
    """
    project = project_id.replace("'", "''")
    type_value = type_id.replace("'", "''")
    return (
        "SQL:(SELECT wi.c_uri FROM workitem wi WHERE wi.c_uri IN ("  # noqa: S608
        "SELECT MIN(cf.fk_uri_workitem) FROM cf_workitem cf "
        "INNER JOIN workitem w2 ON w2.c_uri = cf.fk_uri_workitem "
        "INNER JOIN project p ON w2.fk_uri_project = p.c_uri "
        f"WHERE p.c_id = '{project}' AND w2.c_type = '{type_value}' "
        "GROUP BY cf.c_name))"
    )
