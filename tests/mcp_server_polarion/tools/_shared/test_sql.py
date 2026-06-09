"""Direct unit tests for the REST-SQL query builders.

The builders interpolate caller-supplied ids straight into a ``SQL:(...)``
string with no bind parameters, so the ``'``-doubling escape is the only thing
standing between a malicious id and an injected clause. These tests pin that
escape and the overall query shape; the guard/document tools exercise the
builders' behaviour transitively against `mock_client`.
"""

from __future__ import annotations

from mcp_server_polarion.tools._shared.sql import (
    one_heading_per_document_sql,
    one_item_per_custom_field_sql,
)


class TestOneHeadingPerDocumentSql:
    def test_shape(self) -> None:
        sql = one_heading_per_document_sql("PROJ")
        assert sql.startswith("SQL:(SELECT MIN(wi.c_uri) FROM workitem wi")
        assert "INNER JOIN module mod ON wi.fk_uri_module = mod.c_uri" in sql
        assert "p.c_id = 'PROJ'" in sql
        assert "wi.c_type = 'heading'" in sql
        assert "wi.c_deleted IS NOT TRUE" in sql
        assert sql.endswith("GROUP BY mod.c_uri)")

    def test_escapes_single_quote_in_project(self) -> None:
        sql = one_heading_per_document_sql("PR'OJ")
        # Doubled, so the id stays inside its quoted literal -- no clause break.
        assert "p.c_id = 'PR''OJ'" in sql

    def test_injection_attempt_stays_quoted(self) -> None:
        sql = one_heading_per_document_sql("x' OR '1'='1")
        assert "p.c_id = 'x'' OR ''1''=''1'" in sql


class TestOneItemPerCustomFieldSql:
    def test_shape(self) -> None:
        sql = one_item_per_custom_field_sql("PROJ", "requirement")
        assert sql.startswith("SQL:(SELECT wi.c_uri FROM workitem wi WHERE wi.c_uri IN")
        assert "SELECT MIN(cf.fk_uri_workitem) FROM cf_workitem cf" in sql
        assert "p.c_id = 'PROJ'" in sql
        assert "w2.c_type = 'requirement'" in sql
        assert sql.endswith("GROUP BY cf.c_name))")

    def test_escapes_single_quote_in_both_ids(self) -> None:
        sql = one_item_per_custom_field_sql("PR'OJ", "re'q")
        assert "p.c_id = 'PR''OJ'" in sql
        assert "w2.c_type = 're''q'" in sql

    def test_injection_attempt_in_type_stays_quoted(self) -> None:
        sql = one_item_per_custom_field_sql("PROJ", "x' OR '1'='1")
        assert "w2.c_type = 'x'' OR ''1''=''1'" in sql
