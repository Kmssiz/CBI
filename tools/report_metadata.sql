-- Report metadata status for DBeaver (PostgreSQL). Read-only SELECT.
-- Run it, then: result grid > Export data > CSV (UTF-8), and send me the file.
WITH base AS (
    SELECT
        r.id,
        r.name,
        r.path,
        r.pbirs_id,
        r.is_consolide,
        COALESCE(r.report_type, '') AS report_type,
        (SELECT string_agg(o.name, ', ' ORDER BY o.name)
           FROM powerbi_report_reportref_poles x
           JOIN powerbi_report_metadataoption o ON o.id = x.metadataoption_id
          WHERE x.reportref_id = r.id) AS poles,
        (SELECT string_agg(o.name, ', ' ORDER BY o.name)
           FROM powerbi_report_reportref_societes x
           JOIN powerbi_report_metadataoption o ON o.id = x.metadataoption_id
          WHERE x.reportref_id = r.id) AS societes,
        (SELECT string_agg(o.name, ', ' ORDER BY o.name)
           FROM powerbi_report_reportref_directions x
           JOIN powerbi_report_metadataoption o ON o.id = x.metadataoption_id
          WHERE x.reportref_id = r.id) AS directions,
        (SELECT string_agg(o.name, ', ' ORDER BY o.name)
           FROM powerbi_report_reportref_modules x
           JOIN powerbi_report_metadataoption o ON o.id = x.metadataoption_id
          WHERE x.reportref_id = r.id) AS modules,
        u.username AS edited_by
    FROM powerbi_report_reportref r
    LEFT JOIN users_customuser u ON u.id = r.modified_by_id
),
calc AS (
    SELECT
        b.*,
        concat_ws(', ',
            CASE WHEN b.report_type = '' THEN 'Type' END,
            CASE WHEN b.societes IS NULL AND NOT b.is_consolide THEN 'Société' END,
            CASE WHEN b.directions IS NULL AND NOT b.is_consolide THEN 'Direction' END,
            CASE WHEN b.modules IS NULL THEN 'Module' END
        ) AS missing,
        (b.report_type <> '' OR b.poles IS NOT NULL OR b.societes IS NOT NULL
         OR b.directions IS NOT NULL OR b.modules IS NOT NULL OR b.is_consolide) AS given
    FROM base b
)
SELECT
    c.name                                   AS "Report name",
    c.path                                   AS "Path",
    CASE WHEN NOT c.given THEN 'Not given'
         WHEN c.missing <> '' THEN 'Partial'
         ELSE 'Given' END                    AS "Metadata status",
    c.missing                                AS "Missing fields",
    CASE c.report_type
         WHEN 'anomalie'     THEN 'Anomalie'
         WHEN 'bibliotheque' THEN 'Bibliothèque'
         WHEN 'dashboard'    THEN 'Dashboard'
         ELSE c.report_type END              AS "Type",
    CASE WHEN c.is_consolide THEN 'Yes' ELSE 'No' END AS "Consolidé",
    COALESCE(c.poles, '')                    AS "Pôle(s)",
    COALESCE(c.societes, '')                 AS "Société(s)",
    COALESCE(c.directions, '')               AS "Direction(s)",
    COALESCE(c.modules, '')                  AS "Module(s)",
    COALESCE(c.edited_by, '')                AS "Last edited by",
    c.pbirs_id                               AS "PBIRS ID"
FROM calc c
ORDER BY c.path, c.name;
