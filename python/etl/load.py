"""
Load (or update) data from upstream or execute CTAS or add views to Redshift.

A "load" refers to the wholesale replacement of any schema or table involved.

An "update" refers to the gentle replacement of tables.


There are three possibilities:

(1) "Tables" that have upstream sources must have CSV files and a manifest file.

(2) "CTAS" tables are derived from queries so must have a SQL file.  (Think of them
as materialized views.)

(3) "VIEWS" are views and so must have a SQL file.


Details for (2):

Expects for every derived table (CTAS) a SQL file in the S3 bucket with a valid
expression to create the content of the table (meaning: just the select without
closing ';'). The actual DDL statement (CREATE TABLE AS ...) and the table
attributes / constraints are added from the matching table design file.

Note that the table is actually created empty, then CTAS is used for a temporary
table which is then inserted into the table.  This is needed to attach
constraints, attributes, and encodings.
"""

from contextlib import closing
from itertools import chain
import logging
import os.path

import psycopg2

import etl
from etl import TableName
import etl.commands
import etl.config
import etl.dump
import etl.pg
import etl.s3
import etl.schemas


def format_column_list(columns):
    """
    Return string with comma-separated, delimited column names
    """
    return ", ".join('"{}"'.format(column) for column in columns)


def _build_constraints(table_design, exclude_foreign_keys=False):
    constraints = table_design.get("constraints", {})
    ddl_constraints = []
    for pk in ("primary_key", "surrogate_key"):
        if pk in constraints:
            ddl_constraints.append('PRIMARY KEY ( {} )'.format(format_column_list(constraints[pk])))
    for nk in ("unique", "natural_key"):
        if nk in constraints:
            ddl_constraints.append('UNIQUE ( {} )'.format(format_column_list(constraints[nk])))
    if "foreign_key" in constraints and not exclude_foreign_keys:
        local_columns, reference, reference_columns = constraints["foreign_key"]
        reference_table = TableName(*reference.split('.', 1))
        ddl_constraints.append('FOREIGN KEY ( {} ) REFERENCES {} ( {} )'.format(format_column_list(local_columns),
                                                                                reference_table,
                                                                                format_column_list(reference_columns)))
    return ddl_constraints


def _build_attributes(table_design, exclude_distribution=False):
    attributes = table_design.get("attributes", {})
    ddl_attributes = []
    if "distribution" in attributes and not exclude_distribution:
        dist = attributes["distribution"]
        if isinstance(dist, list):
            ddl_attributes.append('DISTSTYLE KEY')
            ddl_attributes.append('DISTKEY ( {} )'.format(format_column_list(dist)))
        elif dist in ("all", "even"):
            ddl_attributes.append('DISTSTYLE {}'.format(dist.upper()))
    if "compound_sort" in attributes:
        ddl_attributes.append('COMPOUND SORTKEY ( {} )'.format(format_column_list(attributes["compound_sort"])))
    elif "interleaved_sort" in attributes:
        ddl_attributes.append('INTERLEAVED SORTKEY ( {} )'.format(format_column_list(attributes["interleaved_sort"])))
    return ddl_attributes


def assemble_table_ddl(table_design, table_name, use_identity=False, is_temp=False):
    """
    Assemble the DDL to create the table for this design.

    Columns must have a name and a SQL type (compatible with Redshift).
    They may have an attribute of the compression encoding and the nullable
    constraint.
    Other column attributes and constraints should be resolved as table
    attributes (e.g. distkey) and table constraints (e.g. primary key).
    Tables may have attributes such as a distribution style and sort key.
    Depending on the distribution style, they may also have a distribution key.
    Supported table constraints include primary key (most likely "id"),
    unique constraint, and foreign keys.
    """
    s_columns = []
    for column in table_design["columns"]:
        if column.get("skipped", False):
            continue
        f_column = '"{name}" {sql_type}'
        if column.get("identity", False) and use_identity:
            f_column += " IDENTITY(1, 1)"
        if "encoding" in column:
            f_column += " ENCODE {encoding}"
        if column.get("not_null", False):
            f_column += " NOT NULL"
        if column.get("references") and not is_temp:
            # Split column constraint into the table and columns that are referenced
            foreign_table, foreign_columns = column["references"]
            column.update({"foreign_table": foreign_table,
                           "foreign_column": format_column_list(foreign_columns)})
            f_column += " REFERENCES {foreign_table} ( {foreign_column} )"
        s_columns.append(f_column.format(**column))
    s_constraints = _build_constraints(table_design, exclude_foreign_keys=is_temp)
    s_attributes = _build_attributes(table_design, exclude_distribution=is_temp)
    table_type = "TEMP TABLE" if is_temp else "TABLE"

    return "CREATE {} IF NOT EXISTS {} (\n{})\n{}".format(table_type, table_name,
                                                          ",\n".join(chain(s_columns, s_constraints)),
                                                          "\n".join(s_attributes)).replace('\n', "\n    ")


def create_table(conn, table_design, table_name, table_owner, drop_table=False, dry_run=False):
    """
    Run the CREATE TABLE statement before trying to copy data into table.
    Also assign ownership to make sure all tables are owned by same user.
    Table may be dropped before (re-)creation but only the table owner is
    allowed to do so.
    """
    logger = logging.getLogger(__name__)
    ddl_stmt = assemble_table_ddl(table_design, table_name)

    if dry_run:
        logger.info("Dry-run: Skipping creation of table '%s'", table_name.identifier)
        logger.debug("Skipped DDL:\n%s", ddl_stmt)
    else:
        if drop_table:
            logger.info("Dropping table '%s'", table_name.identifier)
            etl.pg.execute(conn, "DROP TABLE IF EXISTS {} CASCADE".format(table_name))

        logger.info("Creating table '%s' (if not exists)", table_name.identifier)
        etl.pg.execute(conn, ddl_stmt)

        logger.info("Making user '%s' owner of table '%s'", table_owner, table_name.identifier)
        etl.pg.alter_table_owner(conn, table_name.schema, table_name.table, table_owner)


def create_view(conn, table_design, view_name, table_owner, query_stmt, drop_view=False, dry_run=False):
    """
    Run the CREATE VIEW statement.

    Optionally drop the view first.  This is necessary if the name or type
    of columns changes.
    """
    logger = logging.getLogger(__name__)
    s_columns = format_column_list(column["name"] for column in table_design["columns"])
    ddl_stmt = """CREATE OR REPLACE VIEW {} (\n{}\n) AS\n{}""".format(view_name, s_columns, query_stmt)
    if dry_run:
        logger.info("Dry-run: Skipping creation of view '%s'", view_name.identifier)
        logger.debug("Skipped DDL:\n%s", ddl_stmt)
    else:
        if drop_view:
            logger.info("Dropping view '%s'", view_name.identifier)
            etl.pg.execute(conn, "DROP VIEW IF EXISTS {} CASCADE".format(view_name))
        # TODO Make sure ownership is ETL owner!
        logger.info("Creating view '%s'", view_name.identifier)
        etl.pg.execute(conn, ddl_stmt)


def copy_data(conn, credentials, table_name, bucket_name, csv_files=None, manifest=None, dry_run=False):
    """
    Load data into table in the data warehouse using the COPY command.  Either
    a list of CSV files or a manifest must be provided. Note that instead of
    using the list of files directly, only their longest common prefix is
    used.  (So using a manifest is safer!)

    Tables can only be truncated by their owners, so this will delete all rows
    instead of truncating the tables.
    """
    access = "aws_iam_role={}".format(credentials)
    logger = logging.getLogger(__name__)
    # TODO Only allow uploads with manifest, remove option to load CSV files
    if manifest is not None:
        location = "s3://{}/{}".format(bucket_name, manifest)
        with_manifest = " MANIFEST"
    elif csv_files is not None:
        location = "s3://{}/{}".format(bucket_name, os.path.commonprefix(csv_files))
        with_manifest = ""
    else:
        raise ValueError("Either csv_files or manifest must not be None")
    if dry_run:
        logger.info("Dry-run: Skipping copy for '%s' from%s '%s'", table_name.identifier, with_manifest, location)
    else:
        logger.info("Copying data into '%s' from%s '%s'", table_name.identifier, with_manifest, location)
        try:
            # The connection should not be open with autocommit at this point or we may have empty random tables.
            etl.pg.execute(conn, """DELETE FROM {}""".format(table_name))
            etl.pg.execute(conn, """COPY {}
                                    FROM %s
                                    CREDENTIALS %s{}
                                    FORMAT AS CSV GZIP IGNOREHEADER 1
                                    NULL AS '\\\\N'
                                    TIMEFORMAT AS 'auto' DATEFORMAT AS 'auto'
                                    TRUNCATECOLUMNS
                                 """.format(table_name, with_manifest), (location, access))
            conn.commit()
        except psycopg2.Error as exc:
            conn.rollback()
            if "stl_load_errors" in exc.pgerror:
                logger.debug("Trying to get error message from stl_log_errors table")
                info = etl.pg.query(conn, """SELECT query, starttime, filename, colname, type, col_length,
                                                    line_number, position, err_code, err_reason
                                               FROM stl_load_errors
                                              WHERE session = pg_backend_pid()
                                              ORDER BY starttime DESC
                                              LIMIT 1""")
                values = "  \n".join(["{}: {}".format(k, row[k]) for row in info for k in row.keys()])
                logger.info("Information from stl_load_errors:\n  %s", values)
            raise


def assemble_ctas_ddl(table_design, temp_name, query_stmt):
    """
    Return statement to create table based on a query, something like:
    CREATE TEMP TABLE table_name ( column_name [, ... ] ) table_attributes AS query
    """
    s_columns = format_column_list(column["name"]
                                   for column in table_design["columns"]
                                   if not (column.get("identity", False) or column.get("skipped", False)))
    # TODO Measure whether adding attributes helps or hurts performance.
    s_attributes = _build_attributes(table_design, exclude_distribution=True)
    return "CREATE TEMP TABLE {} (\n{})\n{}\nAS\n".format(temp_name, s_columns,
                                                          "\n".join(s_attributes)).replace('\n', "\n     ") + query_stmt


def assemble_insert_into_dml(table_design, table_name, temp_name, add_row_for_key_0=False):
    """
    Create an INSERT statement to copy data from temp table to new table.

    If there is an identity column involved, also add the n/a row with key=0.
    Note that for timestamps, an arbitrary point in the past is used if the column
    isn't nullable.
    """
    s_columns = format_column_list(column["name"]
                                   for column in table_design["columns"]
                                   if not column.get("skipped", False))
    if add_row_for_key_0:
        na_values_row = []
        for column in table_design["columns"]:
            if column.get("skipped", False):
                continue
            elif column.get("identity", False):
                na_values_row.append(0)
            else:
                # Use NULL for all null-able columns:
                if not column.get("not_null", False):
                    # Use NULL for any nullable column and use type cast (for UNION ALL to succeed)
                    na_values_row.append("NULL::{}".format(column["sql_type"]))
                elif "timestamp" in column["sql_type"]:
                    # XXX Is this a good value or should timestamps be null?
                    na_values_row.append("'0000-01-01 00:00:00'")
                elif "string" in column["type"]:
                    na_values_row.append("'N/A'")
                elif "boolean" in column["type"]:
                    na_values_row.append("FALSE")
                else:
                    na_values_row.append("0")
        s_values = ", ".join(str(value) for value in na_values_row)
        return """INSERT INTO {}
                    (SELECT
                         {}
                       FROM {}
                      UNION ALL
                     SELECT
                         {})""".format(table_name, s_columns, temp_name, s_values).replace('\n', "\n    ")
    else:
        return """INSERT INTO {}
                    (SELECT {}
                       FROM {})""".format(table_name, s_columns, temp_name)


def create_temp_table_as_and_copy(conn, table_name, table_design, query_stmt, add_explain_plan=False, dry_run=False):
    """
    Run the CREATE TABLE AS statement to load data into temp table,
    then copy into final table.

    Actual implementation:
    (1) If there is a column marked with identity=True, then create a temporary
    table, insert into it (to build key values).  Finally insert the temp table
    into the destination table while adding a row that has key=0 and n/a values.
    (2) Otherwise, create temp table with CTAS then copy into destination table.

    Note that CTAS doesn't allow to specify column types (or encoding or column
    constraints) so we need to have a temp table separate from destination
    table in order to have full flexibility how we define the destination table.
    """
    logger = logging.getLogger(__name__)
    temp_identifier = "{}${}".format("staging", table_name.table)
    temp_name = '"{}"'.format(temp_identifier)
    has_any_identity = any([column.get("identity", False) for column in table_design["columns"]])

    if has_any_identity:
        ddl_temp_stmt = assemble_table_ddl(table_design, temp_name, use_identity=True, is_temp=True)
        s_columns = format_column_list(column["name"]
                                       for column in table_design["columns"]
                                       if not (column.get("identity", False) or column.get("skipped", False)))
        dml_temp_stmt = "INSERT INTO {} (\n{}\n) (\n{}\n)".format(temp_name, s_columns, query_stmt)
        dml_stmt = assemble_insert_into_dml(table_design, table_name, temp_name, add_row_for_key_0=True)
    else:
        ddl_temp_stmt = assemble_ctas_ddl(table_design, temp_name, query_stmt)
        dml_temp_stmt = None
        dml_stmt = assemble_insert_into_dml(table_design, table_name, temp_name)

    if add_explain_plan:
        plan = etl.pg.query(conn, "EXPLAIN\n" + query_stmt)
        logger.info("Explain plan for query:\n | %s", "\n | ".join(row[0] for row in plan))
    if dry_run:
        logger.info("Dry-run: Skipping loading of table '%s' using '%s'", table_name.identifier, temp_identifier)
        logger.debug("Skipped DDL for '%s': %s", temp_identifier, ddl_temp_stmt)
        logger.debug("Skipped DML for '%s': %s", temp_identifier, dml_temp_stmt)
        logger.debug("Skipped DML for '%s': %s", table_name.identifier, dml_stmt)
    else:
        logger.info("Creating temp table '%s'", temp_identifier)
        etl.pg.execute(conn, ddl_temp_stmt)
        if dml_temp_stmt:
            logger.info("Filling temp table '%s'", temp_identifier)
            etl.pg.execute(conn, dml_temp_stmt)
        logger.info("Loading table '%s' from temp table '%s'", table_name.identifier, temp_identifier)
        etl.pg.execute(conn, """DELETE FROM {}""".format(table_name))
        etl.pg.execute(conn, dml_stmt)
        etl.pg.execute(conn, """DROP TABLE {}""".format(temp_name))


def grant_access(conn, table_name, etl_group, user_group, dry_run=False):
    """
    Grant select permission to users and all privileges to etl group.
    """
    if dry_run:
        logging.getLogger(__name__).info("Dry-run: Skipping permissions grant on '%s'", table_name.identifier)
    else:
        logging.getLogger(__name__).info("Granting all privileges on '%s' to '%s'", table_name.identifier, etl_group)
        etl.pg.grant_all(conn, table_name.schema, table_name.table, etl_group)
        logging.getLogger(__name__).info("Granting select access on '%s' to '%s'", table_name.identifier, user_group)
        etl.pg.grant_select(conn, table_name.schema, table_name.table, user_group)


def analyze(conn, table_name, dry_run=False):
    """
    Update table statistics.
    """
    if dry_run:
        logging.getLogger(__name__).info("Dry-run: Skipping analysis of '%s'", table_name.identifier)
    else:
        logging.getLogger(__name__).info("Running analyze step on table '%s'", table_name.identifier)
        etl.pg.execute(conn, "ANALYZE {}".format(table_name))


def vacuum(conn, table_name, dry_run=False):
    """
    Final step ... tidy up the warehouse before guests come over.
    """
    if dry_run:
        logging.getLogger(__name__).info("Dry-run: Skipping vacuum of '%s'", table_name.identifier)
    else:
        logging.getLogger(__name__).info("Running vacuum step on table '%s'", table_name.identifier)
        etl.pg.execute(conn, "VACUUM {}".format(table_name))


def load_or_update_redshift(settings, target, prefix, add_explain_plan=False, drop=False, dry_run=True):
    """
    Load table from CSV file or based on SQL query or install new view.

    This is forceful if drop is True ... and replaces anything that might already exist.
    """
    logger = logging.getLogger(__name__)
    dw = etl.config.env_value(settings("data_warehouse", "etl_access"))
    credentials = settings("data_warehouse", "iam_role")

    table_owner = settings("data_warehouse", "owner")
    etl_group = settings("data_warehouse", "groups", "etl")
    user_group = settings("data_warehouse", "groups", "users")

    selection = etl.TableNamePatterns.from_list(target)
    sources = selection.match_field(settings("sources"), "name")
    schemas = [source["name"] for source in sources]

    bucket_name = settings("s3", "bucket_name")
    files_in_s3 = etl.s3.find_files_in_bucket(bucket_name, prefix, schemas, selection)

    if len(files_in_s3) == 0:
        logger.error("No applicable files found in 's3://%s/%s'", bucket_name, prefix)
        return

    vacuumable = []
    with closing(etl.pg.connection(dw)) as conn:
        for source_name in files_in_s3:
            for assoc_table_files in files_in_s3[source_name]:
                table_name = assoc_table_files.target_table_name
                design_file = assoc_table_files.design_file

                with closing(etl.s3.get_file_content(bucket_name, design_file)) as content:
                    table_design = etl.schemas.load_table_design(content, table_name)

                creates = table_design["source_name"] if table_design["source_name"] in ("CTAS", "VIEW") else None
                if creates is not None:
                    with closing(etl.s3.get_file_content(bucket_name, assoc_table_files.sql_file)) as content:
                        query = content.read().decode()

                with conn:
                    if creates == "VIEW":
                        create_view(conn, table_design, table_name, table_owner, query,
                                    drop_view=drop, dry_run=dry_run)
                    elif creates == "CTAS":
                        create_table(conn, table_design, table_name, table_owner, drop_table=drop, dry_run=dry_run)
                        create_temp_table_as_and_copy(conn, table_name, table_design, query,
                                                      add_explain_plan=add_explain_plan, dry_run=dry_run)
                        analyze(conn, table_name, dry_run=dry_run)
                        vacuumable.append(table_name)
                    else:
                        create_table(conn, table_design, table_name, table_owner, drop_table=drop, dry_run=dry_run)
                        copy_data(conn, credentials, table_name, bucket_name, manifest=assoc_table_files.manifest_file,
                                  dry_run=dry_run)
                        analyze(conn, table_name, dry_run=dry_run)
                        vacuumable.append(table_name)
                    grant_access(conn, table_name, etl_group, user_group, dry_run=dry_run)
    # Reconnect to run vacuum outside transaction block
    if not drop:
        with closing(etl.pg.connection(dw, autocommit=True)) as conn:
            for table_name in vacuumable:
                vacuum(conn, table_name, dry_run=dry_run)
