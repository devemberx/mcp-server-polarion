"""REST-SQL ``query`` builders (not public API). No bind parameters — ids
escaped inline by doubling ``'``. A ``SELECT`` over ``workitem`` always yields
work-item resources (column list ignored); callers pair with
``include=``/``fields=`` for the attributes they need.
"""

from __future__ import annotations


def one_heading_per_document_sql(project_id: str) -> str:
    """One representative heading per document (``MIN`` per ``module`` URI).
    ``c_deleted IS NOT TRUE`` excludes the recycle bin (``= 0`` 500s).
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
    """One representative item per custom-field key (``MIN`` per indexed
    ``cf.c_name``) — catches single-item keys a fixed-N sample misses.
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
