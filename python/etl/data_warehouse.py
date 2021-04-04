"""
Module for data warehouse configuration, initialization and user micro management.

There are three large sections:
  - maintaining schemas
  - maintaining (initializing, updating) a database and users (and groups)
  - disconnecting users with open sessions

Note that it is important that the admin access to the ETL is using the `dev` database
and not the data warehouse in many cases. (Can't drop 'development' database if logged into it).

For user management, we require to have passwords for all declared users in a ~/.pgpass file.
"""

import logging
from collections import OrderedDict
from contextlib import closing
from typing import Iterable, List, Optional, Sequence

from psycopg2.extensions import connection as Connection  # only used for typing

import etl.commands
import etl.config
import etl.config.dw
import etl.db
from etl.config.dw import DataWarehouseSchema
from etl.errors import ETLConfigError, ETLRuntimeError
from etl.text import join_with_single_quotes

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def create_schemas(schemas: Iterable[DataWarehouseSchema], use_staging=False, dry_run=False) -> None:
    """
    Create schemas and grant access.

    It's ok if any of the schemas already exist, in which case the owner and privileges are updated.

    This is a callback for a command.
    """
    dsn_etl = etl.config.get_dw_config().dsn_etl
    with closing(etl.db.connection(dsn_etl, autocommit=True, readonly=dry_run)) as conn:
        for schema in schemas:
            create_schema(conn, schema, use_staging=use_staging, dry_run=dry_run)
            if not schema.groups or use_staging:
                # Don't grant usage on staging schemas to readers/writers (if any)
                continue
            grant_access_to_schema(conn, schema, use_staging=use_staging, dry_run=dry_run)


def create_schema(conn, schema, owner=None, use_staging=False, dry_run=False) -> None:
    name = schema.staging_name if use_staging else schema.name
    if dry_run:
        logger.info("Dry-run: Skipping creating schema '%s'", name)
        return

    logger.info("Creating schema '%s'", name)
    etl.db.create_schema(conn, name, owner)
    etl.db.grant_all_on_schema_to_user(conn, name, schema.owner)


def grant_access_to_schema(conn, schema, use_staging=False, dry_run=False) -> None:
    group_names = join_with_single_quotes(schema.groups)
    name = schema.staging_name if use_staging else schema.name
    if dry_run:
        logger.info("Dry-run: Skipping granting access in '%s' to '%s'", name, group_names)
        return

    # Readers/writers are differentiated in table permissions, not schema permissions
    logger.info("Granting access in '%s' to %s", name, group_names)
    etl.db.grant_usage(conn, name, schema.groups)


def filter_to_existing_schemas(
    schemas: Iterable[DataWarehouseSchema], position: Optional[str] = None
) -> List[DataWarehouseSchema]:
    """
    Filter the list of schemas to those that exist (given their position) in their warehouse.

    If the position is omitted (or None, which is the default), then schemas are searched in their
    standard position.
    If the position is "backup", then the schemas are searched in their backup position, which means their name
    is something like "etl_backup$dw".
    If the position is "staging", then the schemas are searched in their staging position, which means their name
    is something like "etl_staging$dw".
    """
    if position == "backup":
        schema_lookup = OrderedDict([(schema.backup_name, schema) for schema in schemas])
    elif position == "staging":
        schema_lookup = OrderedDict([(schema.staging_name, schema) for schema in schemas])
    else:
        schema_lookup = OrderedDict([(schema.name, schema) for schema in schemas])

    dsn_etl = etl.config.get_dw_config().dsn_etl
    with closing(etl.db.connection(dsn_etl, autocommit=True, readonly=True)) as conn:
        found = etl.db.select_schemas(conn, schema_lookup.keys())
        return [schema_lookup[schema_position_name] for schema_position_name in found]


def backup_schemas(schemas: Sequence[DataWarehouseSchema], dry_run=False) -> None:
    """
    For existing schemas, rename them (to their backup position) and drop access privileges.

    Once the access is revoked, the backup schemas "disappear" from BI tools.

    This is a callback for a command.
    """
    dsn_etl = etl.config.get_dw_config().dsn_etl
    with closing(etl.db.connection(dsn_etl, autocommit=True, readonly=dry_run)) as conn:
        found_names = etl.db.select_schemas(conn, [schema.name for schema in schemas])
        if not found_names:
            logger.info("Found no existing schemas to backup")
            return

        if dry_run:
            logger.info(
                "Dry-run: Skipping backup of %d schema(s): %s", len(found_names), join_with_single_quotes(found_names)
            )
            return

        logger.info("Creating backup of %d schema(s): %s", len(found_names), join_with_single_quotes(found_names))
        for schema in [schema for schema in schemas if schema.name in frozenset(found_names)]:
            logger.info("Revoking access from readers and writers to schema '%s' before backup", schema.name)
            revoke_schema_permissions(conn, schema)
            logger.info("Renaming schema '%s' to backup '%s'", schema.name, schema.backup_name)
            etl.db.drop_schema(conn, schema.backup_name)
            etl.db.alter_schema_rename(conn, schema.name, schema.backup_name)


def _promote_schemas(schemas: Sequence[DataWarehouseSchema], from_where: str, dry_run=False) -> None:
    """
    Promote (staging or backup) schemas into their standard names and grant permissions.

    Changes schema.from_name_attr to schema.name where from_name_attr is either "backup_name"
    or "staging_name". So the argument "from_where" must be either "backup" or "staging".
    """
    standard_names = [schema.name for schema in schemas]
    if from_where == "backup":
        from_name_lookup = {schema.name: schema.backup_name for schema in schemas}
    elif from_where == "staging":
        from_name_lookup = {schema.name: schema.staging_name for schema in schemas}
    else:
        raise ValueError("unexpected 'from_where' value")
    if dry_run:
        logger.info(
            "Dry-run: Skipping promotion of %d schema(s) from %s position: %s",
            len(standard_names),
            from_where,
            join_with_single_quotes(standard_names),
        )
        return

    dsn_etl = etl.config.get_dw_config().dsn_etl
    with closing(etl.db.connection(dsn_etl, autocommit=True, readonly=dry_run)) as conn:
        logger.info(
            "Promoting %d schema(s) from %s position: %s",
            len(standard_names),
            from_where,
            join_with_single_quotes(standard_names),
        )
        for schema in schemas:
            logger.info("Renaming schema '%s' from '%s'", schema.name, from_name_lookup[schema.name])
            etl.db.drop_schema(conn, schema.name)
            etl.db.alter_schema_rename(conn, from_name_lookup[schema.name], schema.name)
            logger.info("Granting readers and writers access to schema '%s' after promotion", schema.name)
            grant_schema_permissions(conn, schema)


def restore_schemas_from_backup(schemas: Sequence[DataWarehouseSchema], dry_run=False) -> None:
    """
    For the schemas that we need or want, rename the backups and restore access.

    This is the inverse of backup_schemas. Useful if bad data is in standard schemas.

    This is a callback for a command.
    """
    _promote_schemas(schemas, "backup", dry_run=dry_run)


def publish_schemas_from_staging(schemas: Sequence[DataWarehouseSchema], dry_run=False) -> None:
    """
    Put staging schemas into the standard position.

    Changed with v1.42.0: This no longer backups the schemas from their standard position first.

    This is a callback for a command.
    """
    _promote_schemas(schemas, "staging", dry_run=dry_run)


def grant_schema_permissions(conn: Connection, schema: DataWarehouseSchema) -> None:
    """Grant usage and select on all tables, grant write on all tables only to writers."""
    if schema.groups:
        etl.db.grant_usage(conn, schema.name, schema.groups)
    if schema.reader_groups:
        etl.db.grant_select_on_all_tables_in_schema(conn, schema.name, schema.reader_groups)
    if schema.writer_groups:
        etl.db.grant_select_and_write_on_all_tables_in_schema(conn, schema.name, schema.writer_groups)


def revoke_schema_permissions(conn: Connection, schema: DataWarehouseSchema) -> None:
    """Revoke usage and select on all tables, also revoke write on all tables from writers."""
    if schema.groups:
        etl.db.revoke_usage(conn, schema.name, schema.groups)
        etl.db.revoke_all_on_all_tables_in_schema(conn, schema.name, schema.groups)


def create_groups(dry_run=False) -> None:
    """
    Create all groups from the data warehouse configuration.

    This is a callback for a command.
    """
    config = etl.config.get_dw_config()
    groups = sorted(frozenset(group for schema in config.schemas for group in schema.groups))
    with closing(etl.db.connection(config.dsn_admin_on_etl_db, readonly=dry_run)) as conn:
        _create_groups(conn, groups, dry_run=dry_run)


def _create_groups(conn: Connection, groups: Iterable[str], dry_run=False) -> None:
    """Make sure that all groups in the list exist."""
    found: List[str] = []
    with conn:
        for group in groups:
            if etl.db.group_exists(conn, group):
                found.append(group)
                continue
            if dry_run:
                logger.info("Dry-run: Skipping creating group '%s'", group)
                continue
            logger.info("Creating group '%s'", group)
            etl.db.create_group(conn, group)
    if found:
        logger.info("%d group(s) already existed: %s", len(found), join_with_single_quotes(found))


def _create_or_update_user(conn: Connection, user, only_update=False, dry_run=False):
    """
    Create user in its group, or add user to its group.

    The connection may point to 'dev' database since users are tied to the cluster, not a database.
    """
    with conn:
        if only_update or etl.db.user_exists(conn, user.name):
            if dry_run:
                logger.info("Dry-run: Skipping adding user '%s' to group '%s'", user.name, user.group)
                logger.info("Dry-run: Skipping updating password for user '%s'", user.name)
            else:
                logger.info("Adding user '%s' to group '%s'", user.name, user.group)
                etl.db.alter_group_add_user(conn, user.group, user.name)
                logger.info("Updating password for user '%s'", user.name)
                etl.db.alter_password(conn, user.name, ignore_missing_password=True)
        else:
            if dry_run:
                logger.info("Dry-run: Skipping creating user '%s' in group '%s'", user.name, user.group)
            else:
                logger.info("Creating user '%s' in group '%s'", user.name, user.group)
                etl.db.create_user(conn, user.name, user.group)


def _create_schema_for_user(conn, user, etl_group, dry_run=False):
    user_schema = etl.config.dw.DataWarehouseSchema(
        {"name": user.schema, "owner": user.name, "readers": [user.group, etl_group]}
    )
    create_schema(conn, user_schema, owner=user.name, dry_run=dry_run)
    grant_access_to_schema(conn, user_schema, dry_run=dry_run)


def _update_search_path(conn, user, dry_run=False):
    """Non-system users have their schema in the search path, others get nothing (only "public")."""
    search_path = ["public"]
    if user.schema == user.name:
        search_path[:0] = ["'$user'"]  # needs to be quoted per documentation
    if dry_run:
        logger.info("Dry-run: Skipping setting search path for user '%s' to: %s", user.name, search_path)
        return

    logger.info("Setting search path for user '%s' to: %s", user.name, search_path)
    etl.db.alter_search_path(conn, user.name, search_path)


def initial_setup(with_user_creation=False, force=False, dry_run=False):
    """
    Place named data warehouse database into initial state.

    This destroys the contents of the targeted database.
    You have to set `force` to true if the name of the database doesn't start with 'validation'.

    Optionally use `with_user_creation` flag to create users and groups.

    This is a callback for a command.
    """
    config = etl.config.get_dw_config()
    try:
        database_name = config.dsn_etl["database"]
    except (KeyError, ValueError) as exc:
        raise ETLConfigError("could not identify database initialization target") from exc

    if database_name.startswith("validation"):
        logger.info("Initializing validation database '%s'", database_name)
    elif force:
        logger.info("Initializing non-validation database '%s' forcefully as requested", database_name)
    else:
        raise ETLRuntimeError(
            "Refused to initialize non-validation database '%s' without the --force option" % database_name
        )
    # Create all defined users which includes the ETL user needed before next step (so that
    # database is owned by ETL). Also create all groups referenced in the configuration.
    if with_user_creation:
        groups = sorted(frozenset(group for schema in config.schemas for group in schema.groups))
        with closing(etl.db.connection(config.dsn_admin, readonly=dry_run)) as conn:
            _create_groups(conn, groups, dry_run=dry_run)
            for user in config.users:
                _create_or_update_user(conn, user, dry_run=dry_run)

    owner_name = config.owner.name
    if dry_run:
        logger.info("Dry-run: Skipping drop and create of database '%s' with owner '%s'", database_name, owner_name)
    else:
        with closing(etl.db.connection(config.dsn_admin, autocommit=True)) as conn:
            logger.info("Dropping and creating database '%s' with owner '%s'", database_name, owner_name)
            etl.db.drop_and_create_database(conn, database_name, owner_name)

    with closing(etl.db.connection(config.dsn_admin_on_etl_db, autocommit=True, readonly=dry_run)) as conn:
        if dry_run:
            logger.info("Dry-run: Skipping dropping of PUBLIC schema in '%s'", database_name)
        else:
            logger.info("Dropping PUBLIC schema in '%s'", database_name)
            etl.db.drop_schema(conn, "PUBLIC")
        if with_user_creation:
            for user in config.users:
                if user.schema:
                    _create_schema_for_user(conn, user, config.groups[0], dry_run=dry_run)
                _update_search_path(conn, user, dry_run=dry_run)


def create_or_update_user(user_name, group_name=None, add_user_schema=False, only_update=False, dry_run=False):
    """
    Add new user to cluster or update existing user.

    Either pick a group or accept the default group (from settings).
    If the group does not yet exist, then we create the user's group here.

    If so advised, creates a schema for the user, making sure that the ETL user keeps read access
    via its group. So this assumes that the connection string points to the ETL database, not 'dev'.
    """
    config = etl.config.get_dw_config()
    # Find user in the list of pre-defined users or create new user instance with default settings
    for user in config.users:
        if user.name == user_name:
            break
    else:
        info = {"name": user_name, "group": group_name or config.default_group}
        if add_user_schema:
            info["schema"] = user_name
        user = etl.config.dw.DataWarehouseUser(info)

    if user.name == "default":
        raise ValueError("illegal user name '%s'" % user.name)
    if user.group not in config.groups and user.group != config.default_group:
        raise ValueError("specified group ('%s') not present in DataWarehouseConfig" % user.group)

    with closing(etl.db.connection(config.dsn_admin_on_etl_db, readonly=dry_run)) as conn:
        _create_groups(conn, [user.group], dry_run=dry_run)
        _create_or_update_user(conn, user, only_update=only_update, dry_run=dry_run)

        with conn:
            if add_user_schema:
                _create_schema_for_user(conn, user, config.groups[0], dry_run=dry_run)
            elif user.schema is not None:
                logger.warning(
                    "User '%s' has schema '%s' configured but adding that was not requested", user.name, user.schema
                )
            _update_search_path(conn, user, dry_run=dry_run)


def create_new_user(new_user, group=None, add_user_schema=False, dry_run=False):
    """
    Create a new user in the cluster, optionally adding them to a group.

    This is a callback for a command.
    """
    create_or_update_user(new_user, group, add_user_schema=add_user_schema, only_update=False, dry_run=dry_run)


def update_user(old_user, group=None, add_user_schema=False, dry_run=False):
    """
    Update a user in the cluster, optionally adding them to a group.

    This is a callback for a command.
    """
    create_or_update_user(old_user, group, add_user_schema=add_user_schema, only_update=True, dry_run=dry_run)


def list_open_transactions(cx: Connection):
    """
    Look for sessions that by other users that might interfere with the ETL.

    This returns information about sessions (identified by the PIDs of the backends)
    that have locks open and are for the same database as the current sessions.
    (Also, sessions of the current user are skipped so that we don't bounce ourselves.)
    """
    stmt = """
        SELECT proc_pid
             , txn_db
             , txn_owner
             , txn_start
             , LISTAGG(table_name, ', ') WITHIN GROUP (ORDER BY table_name) AS tables
          FROM (
            SELECT DISTINCT
                   pid AS proc_pid
                 , txn_db
                 , txn_owner
                 , txn_start
                 , COALESCE(pn.nspname || '.' || pc.relname, 'Unknown') AS table_name
              FROM pg_catalog.svv_transactions AS st
              LEFT JOIN pg_catalog.pg_class AS pc ON st.relation = pc.oid
              LEFT JOIN pg_catalog.pg_namespace AS pn ON pc.relnamespace = pn.oid
             WHERE txn_owner <> current_user
               AND txn_db = current_database()
               ) t
         GROUP BY proc_pid, txn_db, txn_owner, txn_start
         ORDER BY proc_pid, txn_db, txn_owner, txn_start, tables
        """
    return etl.db.query(cx, stmt)


def terminate_sessions_with_transaction_locks(cx: Connection, dry_run=False) -> None:
    """
    Call Redshift's PG_TERMINATE_BACKEND to kick out other users with running queries.

    Other queries might interfere with the ETL, e.g. by having locks.
    """
    tx_info = list_open_transactions(cx)
    etl.db.print_result("List of sessions that have open transactions:", tx_info)
    pids = sorted({row["proc_pid"] for row in tx_info})
    logger.debug("List of %d session PID(s): %s", len(pids), pids)
    for pid in pids:
        msg = "Terminate session with backend {:d} holding transaction locks".format(pid)
        term = "SELECT PG_TERMINATE_BACKEND({:d})".format(pid)
        etl.db.run(cx, msg, term, dry_run=dry_run)


def terminate_sessions(dry_run=False) -> None:
    """
    Terminate sessions that currently hold locks on (user or system) tables.

    This is a callback for a command.
    """
    dsn_admin = etl.config.get_dw_config().dsn_admin_on_etl_db
    with closing(etl.db.connection(dsn_admin, autocommit=True)) as conn:
        etl.db.execute(conn, "SET query_group TO 'superuser'")
        terminate_sessions_with_transaction_locks(conn, dry_run=dry_run)
