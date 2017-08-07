#! /usr/bin/env python3

"""
Thin wrapper around Psycopg2 for database access, adding niceties like logging,
transaction handling, simple DSN strings, as well as simple commands
(for new users, schema creation, etc.)

For a description of the connection string, take inspiration from:
https://www.postgresql.org/docs/9.4/static/libpq-connect.html#LIBPQ-CONNSTRING
"""

import inspect
import logging
import os
import os.path
import re
import textwrap
from contextlib import closing, contextmanager
from typing import Dict, List, Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool
import pgpasslib

from etl.timer import Timer

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def parse_connection_string(dsn: str) -> Dict[str, str]:
    """
    Extract connection value from JDBC-style connection string.

    The fields ""host" and "database" will be set.
    The fields "user", "password", "port" and "sslmode" may be set.

    >>> dsn_min = parse_connection_string("postgres://example.com/xyzzy")
    >>> unparse_connection(dsn_min)
    'host=example.com port=<default> dbname=xyzzy user=<default> password=***'
    >>> dsn_max = parse_connection_string("postgresql://john.doe:secret@pg.example.com:5432/xyzzy")
    >>> unparse_connection(dsn_max)
    'host=pg.example.com port=5432 dbname=xyzzy user=john.doe password=***'
    """
    # Some people, when confronted with a problem, think "I know, I'll use regular expressions."
    # Now they have two problems.
    dsn_re = re.compile(r"""(?:jdbc:)?(redshift|postgresql|postgres)://  # be nice and accept either connection type
                            (?:(?P<user>\w[.\w]*)(?::(?P<password>[-\w]+))?@)?  # optional user with password
                            (?P<host>\w[-.\w]*)(:?:(?P<port>\d+))?/  # host and optional port information
                            (?P<database>\w+)  # database (and not dbname)
                            (?:\?sslmode=(?P<sslmode>\w+))?$""",  # sslmode is the only option currently supported
                        re.VERBOSE)
    dsn_after_expansion = os.path.expandvars(dsn)  # Supports stuff like $USER
    match = dsn_re.match(dsn_after_expansion)
    if match is None:
        raise ValueError("value of connection string does not conform to expected format.")
    values = match.groupdict()
    return {key: values[key] for key in values if values[key] is not None}


def unparse_connection(dsn: Dict[str, str]) -> str:
    """
    Return connection string for pretty printing or copying when starting psql
    """
    values = dict(dsn)
    for key in ("user", "port"):
        if key not in values:
            values[key] = "<default>"
    return "host={host} port={port} dbname={database} user={user} password=***".format(**values)


def connection(dsn_dict: Dict[str, str], application_name=psycopg2.__name__, autocommit=False, readonly=False):
    """
    Open a connection to the database described by dsn_string which looks something like
    "postgresql://user:password@host:port/database" (see parse_connection_string).

    Caveat Emptor: By default, this turns off autocommit on the connection. This means that you
    have to explicitly commit on the connection object or run your SQL within a transaction context!
    """
    dsn_values = dict(dsn_dict, application_name=application_name, cursor_factory=psycopg2.extras.DictCursor)
    logger.info("Connecting to: %s", unparse_connection(dsn_values))
    cx = psycopg2.connect(**dsn_values)
    cx.set_session(autocommit=autocommit, readonly=readonly)
    logger.debug("Connected successfully (backend pid: %d, server version: %s, is_superuser: %s)",
                 cx.get_backend_pid(), cx.server_version, cx.get_parameter_status("is_superuser"))
    return cx


def connection_pool(max_conn, dsn_dict: Dict[str, str], application_name=psycopg2.__name__):
    """
    Create a connection pool (with up to max_conn connections), where all connections will use the
    given connection string.
    """
    dsn_values = dict(dsn_dict, application_name=application_name, cursor_factory=psycopg2.extras.DictCursor)
    return psycopg2.pool.ThreadedConnectionPool(1, max_conn, **dsn_values)


def extract_dsn(dsn_dict: Dict[str, str], read_only=False):
    """
    Break the connection string into a JDBC URL and connection properties.

    This is necessary since a JDBC URL may not contain all the properties needed
    to successfully connect, e.g. username, password.  These properties must
    be passed in separately.
    """
    dsn_properties = dict(dsn_dict)  # so as to not mutate the argument
    dsn_properties.update({
        "ApplicationName": __name__,
        "readOnly": "true" if read_only else "false",
        "driver": "org.postgresql.Driver"  # necessary, weirdly enough
    })
    if "port" in dsn_properties:
        jdbc_url = "jdbc:postgresql://{host}:{port}/{database}".format(**dsn_properties)
    else:
        jdbc_url = "jdbc:postgresql://{host}/{database}".format(**dsn_properties)
    return jdbc_url, dsn_properties


def dbname(cx):
    """
    Return name of database that this connection points to.
    """
    dsn = dict(kv.split('=') for kv in cx.dsn.split(" "))
    return dsn["dbname"]


def remove_password(s):
    """
    Remove any password or credentials information from a query string.

    >>> s = '''CREATE USER dw_user IN GROUP etl PASSWORD 'horse_staple_battery';'''
    >>> remove_password(s)
    "CREATE USER dw_user IN GROUP etl PASSWORD '';"
    >>> s = '''copy listing from 's3://mybucket/data/listing/' credentials 'aws_access_key_id=...';'''
    >>> remove_password(s)
    "copy listing from 's3://mybucket/data/listing/' credentials '';"
    >>> s = '''COPY LISTING FROM 's3://mybucket/data/listing/' CREDENTIALS 'aws_iam_role=...';'''
    >>> remove_password(s)
    "COPY LISTING FROM 's3://mybucket/data/listing/' CREDENTIALS '';"
    """
    match = re.search("(CREDENTIALS|PASSWORD)\s*'([^']*)'", s, re.IGNORECASE)
    if match:
        start, end = match.span()
        creds = match.groups()[0]
        s = s[:start] + creds + " ''" + s[end:]
    return s


def mogrify(cursor, stmt, args=()):
    """
    Build the statement by filling in the arguments (and cleaning up whitespace along the way).
    """
    stripped = textwrap.dedent(stmt).strip('\n')
    if len(args):
        actual_stmt = cursor.mogrify(stripped, args)
    else:
        actual_stmt = cursor.mogrify(stripped)
    return actual_stmt


def query(cx, stmt, args=()):
    """
    Send query stmt to connection (with parameters) and return rows.
    """
    return execute(cx, stmt, args, return_result=True)


def execute(cx, stmt, args=(), return_result=False):
    """
    Execute query in 'stmt' over connection 'cx' (with parameters in 'args').

    Be careful with query statements that have a '%' in them (say for LIKE)
    since this will interfere with psycopg2 interpreting parameters.

    Printing the query will not print AWS credentials IF the string used matches "CREDENTIALS '[^']*'"
    So be careful or you'll end up sending your credentials to the logfile.
    """
    with cx.cursor() as cursor:
        executable_statement = mogrify(cursor, stmt, args)
        printable_stmt = remove_password(executable_statement.decode())
        logger.debug("QUERY:\n%s\n;", printable_stmt)
        with Timer() as timer:
            cursor.execute(executable_statement)
        if cursor.rowcount is not None and cursor.rowcount > 0:
            logger.debug("QUERY STATUS: %s [rowcount=%d] (%s)", cursor.statusmessage, cursor.rowcount, timer)
        else:
            logger.debug("QUERY STATUS: %s (%s)", cursor.statusmessage, timer)
        if cx.notices and logger.isEnabledFor(logging.DEBUG):
            for msg in cx.notices:
                logger.debug("QUERY " + msg.rstrip('\n'))
            del cx.notices[:]
        if return_result:
            return cursor.fetchall()


def skip_query(cx, stmt, args=()):
    """
    For logging side-effect only ... show which query would have been executed.
    """
    with cx.cursor() as cursor:
        executable_statement = mogrify(cursor, stmt, args)
        printable_stmt = remove_password(executable_statement.decode())
        logger.debug("Skipped QUERY:\n%s\n;", printable_stmt)


def run(cx, message, stmt, args=(), return_result=False, dry_run=False):
    """
    Execute the query and log the message around it.  Or just show what would have been run in dry-run mode.
    """
    # Figure out caller for better logging
    current_frame = inspect.currentframe()
    caller_code = current_frame.f_back.f_code
    caller_name = caller_code.co_name

    if dry_run:
        logger.info("({}) Dry-run: Skipping {}{}".format(caller_name, message[:1].lower(), message[1:]))
        skip_query(cx, stmt, args=args)
    else:
        logger.info("({}) {}".format(caller_name, message))
        return execute(cx, stmt, args=args, return_result=return_result)


def format_result(dict_rows) -> str:
    """
    Take result from query() and pretty-format it into one string, ready for print or log.
    """
    keys = list(dict_rows[0].keys())
    content = [keys]  # header
    for row in dict_rows:
        content.append([
            str(row[k]).strip() for k in keys
        ])
    return '\n'.join([', '.join(c) for c in content])


def explain(cx, stmt, args=()):
    """
    Return explain plan for the query as a list of steps.

    We sometimes use this just to test out a query syntax so we are heavy on the logging.
    """
    rows = execute(cx, "EXPLAIN\n" + stmt, args, return_result=True)
    lines = [row[0] for row in rows]
    logger.debug("Query plan:\n | " + "\n | ".join(lines))
    return lines


def test_connection(cx):
    """
    Send a test query to our connection
    """
    is_alive = False
    try:
        result = run(cx, "Ping {}!".format(dbname(cx)), "SELECT 1 AS connection_test", return_result=True)
        if len(result) == 1 and "connection_test" in result[0]:
            is_alive = cx.closed == 0
    except psycopg2.OperationalError:
        return False
    else:
        return is_alive


def ping(dsn):
    """
    Give me a ping to the database, Vasili. One ping only, please.
    """
    with closing(connection(dsn, readonly=True)) as cx:
        if test_connection(cx):
            print("{} is alive".format(dbname(cx)))


def log_sql_error(exc):
    """
    Send information from psycopg2.Error instance to logfile.

    See PostgreSQL documentation at
    http://www.postgresql.org/docs/current/static/libpq-exec.html#LIBPQ-PQRESULTERRORFIELD
    and psycopg2 documentation at http://initd.org/psycopg/docs/extensions.html
    """
    if exc.pgcode is not None:
        logger.error('SQL ERROR "%s" %s', exc.pgcode, str(exc.pgerror).strip())
    for name in ('severity',
                 'sqlstate',
                 'message_primary',
                 'message_detail',
                 'message_hint',
                 'statement_position',
                 'internal_position',
                 'internal_query',
                 'context',
                 'schema_name',
                 'table_name',
                 'column_name',
                 'datatype_name',
                 'constraint_name',
                 # 'source_file',
                 # 'source_function',
                 # 'source_line',
                 ):
        value = getattr(exc.diag, name, None)
        if value:
            logger.debug("DIAG %s: %s", name.upper(), value)


@contextmanager
def log_error():
    """Log any psycopg2 errors using the pretty log_sql_error function before re-raising the exception"""
    try:
        yield
    except psycopg2.Error as exc:
        log_sql_error(exc)
        raise


# ---- DATABASE ----

def drop_and_create_database(cx, database, owner):
    exists = query(cx, """SELECT 1 FROM pg_catalog.pg_database WHERE datname = '{}'""".format(database))
    if exists:
        execute(cx, """DROP DATABASE {}""".format(database))
    execute(cx, """CREATE DATABASE {} WITH OWNER {}""".format(database, owner))


# ---- USERS and GROUPS ----

def create_group(cx, group):
    execute(cx, """CREATE GROUP "{}" """.format(group))


def create_user(cx, user, group):
    dsn_complete = dict(kv.split('=') for kv in cx.dsn.split(" "))
    dsn_partial = {key: dsn_complete[key] for key in ["host", "port", "dbname"]}
    password = pgpasslib.getpass(user=user, **dsn_partial)
    if password is None:
        raise RuntimeError("Password missing from PGPASSFILE for {}".format(user))
    execute(cx, """CREATE USER {} IN GROUP "{}" PASSWORD %s""".format(user, group), (password,))


def alter_group_add_user(cx, group, user):
    execute(cx, """ALTER GROUP {} ADD USER "{}" """.format(group, user))


def alter_search_path(cx, user, schemas):
    execute(cx, """ALTER USER {} SET SEARCH_PATH TO {}""".format(user, ', '.join(schemas)))


def set_search_path(cx, schemas):
    execute(cx, """SET SEARCH_PATH = {}""".format(', '.join(schemas)))


def list_connections(cx):
    return query(cx, """SELECT datname, procpid, usesysid, usename
                          FROM pg_catalog.pg_stat_activity""")


def list_transactions(cx):
    return query(cx, """SELECT t.*
                             , c.relname
                          FROM pg_catalog.svv_transactions t
                          JOIN pg_catalog.pg_class c ON t.relation = c.OID""")


# ---- SCHEMAS ----

def select_schemas(cx, names) -> List[str]:
    rows = query(cx, """
        SELECT nspname AS name
          FROM pg_catalog.pg_namespace
         WHERE nspname IN %s
        """, (tuple(names),))
    found = frozenset(row[0] for row in rows)
    # Instead of an ORDER BY clause, keep original order.
    return [name for name in names if name in found]


def drop_schema(cx, name):
    execute(cx, """DROP SCHEMA IF EXISTS "{}" CASCADE""".format(name))


def alter_schema_rename(cx, old_name, new_name):
    execute(cx, """ALTER SCHEMA {} RENAME TO "{}" """.format(old_name, new_name))


def create_schema(cx, schema, owner=None):
    execute(cx, """CREATE SCHEMA IF NOT EXISTS "{}" """.format(schema))
    if owner:
        # Because of the "IF NOT EXISTS" we need to expressly set owner in case there's a change in ownership.
        execute(cx, """ALTER SCHEMA "{}" OWNER TO "{}" """.format(schema, owner))


def grant_usage(cx, schema, group):
    execute(cx, """GRANT USAGE ON SCHEMA "{}" TO GROUP "{}" """.format(schema, group))


def grant_all_on_schema_to_user(cx, schema, user):
    execute(cx, """GRANT ALL PRIVILEGES ON SCHEMA "{}" TO "{}" """.format(schema, user))


def revoke_usage(cx, schema, group):
    execute(cx, """REVOKE USAGE ON SCHEMA "{}" FROM GROUP "{}" """.format(schema, group))


def grant_select_on_all_tables_in_schema(cx, schema, group):
    execute(cx, """GRANT SELECT ON ALL TABLES IN SCHEMA "{}" TO GROUP "{}" """.format(schema, group))


def revoke_select_on_all_tables_in_schema(cx, schema, group):
    execute(cx, """REVOKE SELECT ON ALL TABLES IN SCHEMA "{}" FROM GROUP "{}" """.format(schema, group))


def grant_select_and_write_on_all_tables_in_schema(cx, schema, group):
    execute(cx,
            """GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{}" TO GROUP "{}" """.format(
                schema, group)
            )


def revoke_select_and_write_on_all_tables_in_schema(cx, schema, group):
    execute(cx,
            """REVOKE SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{}" FROM GROUP "{}" """.format(
                schema, group)
            )


# ---- TABLES ----

def relation_kind(cx, schema, table) -> Optional[str]:
    """
    Return "kind" of relation, either 'TABLE' or 'VIEW' for relations that actually exist.
    If the relation doesn't exist, None is returned.
    """
    rows = query(cx, """
        SELECT CASE cls.relkind
                 WHEN 'r' THEN 'TABLE'
                 WHEN 'v' THEN 'VIEW'
               END AS relation_kind
          FROM pg_catalog.pg_class AS cls
          JOIN pg_catalog.pg_namespace AS nsp ON cls.relnamespace = nsp.oid
         WHERE nsp.nspname = %s
           AND cls.relname = %s
           AND cls.relkind IN ('r', 'v')
        """, (schema, table))
    if rows:
        return rows[0][0]
    else:
        return None


def grant_select(cx, schema, table, group):
    execute(cx, """GRANT SELECT ON "{}"."{}" TO GROUP "{}" """.format(schema, table, group))


def grant_select_and_write(cx, schema, table, group):
    execute(cx, """GRANT SELECT, INSERT, UPDATE, DELETE ON "{}"."{}" TO GROUP "{}" """.format(schema, table, group))


def grant_all_to_user(cx, schema, table, user):
    execute(cx, """GRANT ALL PRIVILEGES ON "{}"."{}" TO "{}" """.format(schema, table, user))


def revoke_select(cx, schema, table, group):
    execute(cx, """REVOKE SELECT ON "{}"."{}" FROM GROUP "{}" """.format(schema, table, group))


def alter_table_owner(cx, schema, table, owner):
    execute(cx, """ALTER TABLE "{}"."{}" OWNER TO {} """.format(schema, table, owner))


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: {} dsn_string".format(sys.argv[0]))
        print()
        print('Hint: Try your local machine: {} "postgres://${{USER}}@localhost:5432/${{USER}}"'.format(sys.argv[0]))
        sys.exit(1)

    logging.basicConfig(level=logging.DEBUG)
    dsn_dict_ = parse_connection_string(sys.argv[1])
    with log_error():
        ping(dsn_dict_)