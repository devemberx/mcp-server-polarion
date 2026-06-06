# list_work_items SQL recipes

Copy-paste recipes for the ``SQL:(...)`` prefix of ``list_work_items``.
Substitute ``<placeholders>`` and escape ``'`` as ``''`` (no bind params).
``LIKE`` stays in the top-level ``WHERE`` via ``INNER JOIN`` because Polarion
rejects it inside ``EXISTS (SELECT ...)``.

## Schema (tables are ``POLARION.<name>``; columns used for JOINs / filtering)

    WORKITEM     c_uri, c_id, c_type, c_title, c_status,
                 fk_uri_module, fk_uri_project
    MODULE       c_uri, c_id, c_modulefolder, fk_uri_project
    PROJECT      c_uri, c_id
    REL_MODULE_WORKITEM   fk_uri_module, fk_uri_workitem
    CF_WORKITEM           fk_uri_workitem, c_name,
                          c_string_value | c_boolean_value |
                          c_durationtime_value | ...
    STRUCT_WORKITEM_LINKEDWORKITEMS
                 fk_uri_p_workitem (source / role-holder),
                 fk_uri_workitem   (target), c_role

## Recipe 1 — work items belonging to a document

Excludes Recycle Bin and Referenced Work Items, matching
``read_document_parts`` scope.

    SQL:(SELECT item.* FROM POLARION.MODULE doc
    INNER JOIN POLARION.PROJECT proj ON doc.FK_URI_PROJECT = proj.C_URI
    INNER JOIN POLARION.REL_MODULE_WORKITEM rel
        ON rel.FK_URI_MODULE = doc.C_URI
    INNER JOIN POLARION.WORKITEM item
        ON item.C_URI = rel.FK_URI_WORKITEM
    WHERE proj.C_ID = '<project>' AND doc.C_MODULEFOLDER = '<space>'
    AND doc.C_ID = '<document>'
    AND item.FK_URI_MODULE = doc.C_URI)

The trailing ``item.FK_URI_MODULE = doc.C_URI`` predicate excludes Referenced
Work Items and Recycle Bin entries; drop it to include them. Common tweaks:
exclude headings with ``AND item.C_TYPE != 'heading'``, filter type with
``AND item.C_TYPE IN ('requirement','testcase')``, or substring-match the
title with ``AND LOWER(item.C_TITLE) LIKE '%foo%'``.

## Recipe 2 — custom-field value search (``=`` or ``LIKE``)

    SQL:(SELECT item.* FROM POLARION.WORKITEM item
    INNER JOIN POLARION.CF_WORKITEM cf ON cf.FK_URI_WORKITEM = item.C_URI
    WHERE cf.C_NAME = '<field>'
    AND cf.C_STRING_VALUE LIKE '%<value>%')

Use ``c_boolean_value`` / ``c_durationtime_value`` (etc.) for non-string
customs. Rich-text customs are CLOB-stored — read them via ``get_work_item``.

## Recipe 3 — back-traceability (items whose role link points to a target)

    SQL:(SELECT DISTINCT item.* FROM POLARION.WORKITEM item
    INNER JOIN POLARION.STRUCT_WORKITEM_LINKEDWORKITEMS link
        ON link.FK_URI_P_WORKITEM = item.C_URI
    INNER JOIN POLARION.WORKITEM target
        ON target.C_URI = link.FK_URI_WORKITEM
    WHERE target.C_ID = '<target-id>' AND link.C_ROLE = '<role>')

``fk_uri_p_workitem`` is the source (role-holder), ``fk_uri_workitem`` the
target. Unlike ``list_work_item_links(direction='back')`` this preserves
``c_role``.

## Recipe 4 — forward-traceability (items linked from a source)

    SQL:(SELECT DISTINCT item.* FROM POLARION.WORKITEM item
    INNER JOIN POLARION.STRUCT_WORKITEM_LINKEDWORKITEMS link
        ON link.FK_URI_WORKITEM = item.C_URI
    INNER JOIN POLARION.WORKITEM source
        ON source.C_URI = link.FK_URI_P_WORKITEM
    WHERE source.C_ID = '<source-id>' AND link.C_ROLE = '<role>')

Mirror of Recipe 3 with the two ``FK_*_WORKITEM`` columns swapped in both JOINs.

## More

Testrun / timepoint / assignee joins and the ``LUCENE_QUERY`` table function
live in the Polarion SDK at ``polarion/sdk/doc/database/SQLQueryExamples.pdf``.
