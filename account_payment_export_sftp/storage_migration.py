# Copyright 2026 Compassion CH (<https://www.compassion.ch>)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Give an sftp fs.storage to the EDI backends that have none.

Such a backend has no ``storage_id``, but its column ``LEGACY_COLUMN`` holds
the id of a ``storage_backend`` row with the server settings. This code
creates the fs.storage from these settings and links the EDI backends and
their exchange records to it.

It never connects to the server. It never logs a setting value: settings
hold credentials.
"""

import json
import logging

from odoo.tools import SQL
from odoo.tools.sql import column_exists, table_exists

_logger = logging.getLogger(__name__)

LEGACY_COLUMN = "openupgrade_legacy_16_0_storage_id"
SETTING_FIELDS = (
    "backend_type",
    "directory_path",
    "sftp_auth_method",
    "sftp_login",
    "sftp_password",
    "sftp_port",
    "sftp_server",
    "sftp_ssh_private_key",
)


def migrate_edi_storage(env):
    """Link EDI backends that have no fs.storage to a new one.

    Safe to run twice: backends that already have a storage are not changed.
    """
    cr = env.cr
    if not table_exists(cr, "storage_backend") or not column_exists(
        cr, "edi_backend", LEGACY_COLUMN
    ):
        _logger.info("EDI storage migration: no legacy storage column, nothing to do")
        return
    # The SQL below must see the pending ORM changes.
    env.flush_all()
    cr.execute(
        SQL(
            """
            SELECT %(legacy)s, array_agg(id ORDER BY id)
            FROM edi_backend
            WHERE storage_id IS NULL AND %(legacy)s IS NOT NULL
            GROUP BY %(legacy)s
            ORDER BY %(legacy)s
            """,
            legacy=SQL.identifier(LEGACY_COLUMN),
        )
    )
    rows = cr.fetchall()
    if not rows:
        _logger.info("EDI storage migration: all EDI backends have a storage")
        return
    for storage_backend_id, backend_ids in rows:
        storage = _get_linked_storage(env, storage_backend_id)
        if not storage:
            storage = _create_sftp_storage(env, storage_backend_id)
        if not storage:
            continue
        backends = (
            env["edi.backend"].with_context(active_test=False).browse(backend_ids)
        )
        backends.write({"storage_id": storage.id})
        backends.flush_recordset()
        # Each exchange record gets the storage of its backend.
        cr.execute(
            SQL(
                """
                UPDATE edi_exchange_record
                SET storage_id = %s
                WHERE backend_id IN %s AND storage_id IS NULL
                """,
                storage.id,
                tuple(backend_ids),
            )
        )
        record_count = cr.rowcount
        env["edi.exchange.record"].invalidate_model(["storage_id"])
        _logger.info(
            "EDI storage migration: storage.backend %s -> fs.storage %s, "
            "%s EDI backends %s, %s exchange records",
            storage_backend_id,
            storage.id,
            len(backends),
            backends.ids,
            record_count,
        )
        _check_exchange_types(env, backends)
        _log_pending_output_records(env, backends)


def _get_linked_storage(env, storage_backend_id):
    """Return the fs.storage already used for the same storage.backend, if any.

    This happens when only some backends were linked before.
    """
    env.cr.execute(
        SQL(
            """
            SELECT storage_id FROM edi_backend
            WHERE %s = %s AND storage_id IS NOT NULL
            ORDER BY id
            LIMIT 1
            """,
            SQL.identifier(LEGACY_COLUMN),
            storage_backend_id,
        )
    )
    row = env.cr.fetchone()
    return env["fs.storage"].browse(row[0]) if row else env["fs.storage"]


def _create_sftp_storage(env, storage_backend_id):
    env.cr.execute(
        SQL("SELECT * FROM storage_backend WHERE id = %s", storage_backend_id)
    )
    row = env.cr.dictfetchone()
    if not row:
        _logger.warning(
            "EDI storage migration: storage.backend %s not found", storage_backend_id
        )
        return env["fs.storage"]
    name = row["name"]
    settings = read_backend_settings(row)
    backend_type = settings.get("backend_type")
    if backend_type != "sftp":
        _logger.warning(
            "EDI storage migration: storage.backend %s has type %s, "
            "only sftp is migrated. Please create the fs.storage by hand.",
            storage_backend_id,
            backend_type,
        )
        return env["fs.storage"]
    options = get_sftp_options(settings)
    empty = sorted(key for key, value in options.items() if value in (None, ""))
    if empty:
        # The values can come from a server environment file instead of the
        # database: then set them in its section [fs_storage.<code>].
        _logger.warning(
            "EDI storage migration: storage.backend %s, empty sftp options: %s",
            storage_backend_id,
            ", ".join(empty),
        )
    storage = env["fs.storage"].create(
        {
            "name": name,
            "code": _get_free_code(env, name, storage_backend_id),
            "protocol": "sftp",
            "options": json.dumps(options),
            "directory_path": settings.get("directory_path") or False,
        }
    )
    # "ls" only reads: the default "marker_file" writes a file at the root of
    # the server each time the storage is used, and a bank server may refuse
    # it. Write it after create: in create, the default of its server
    # environment field wins over a given value.
    storage.write({"check_connection_method": "ls"})
    env.registry.clear_cache()
    _logger.info(
        "EDI storage migration: fs.storage %s created (sftp, auth %s, options %s)",
        storage.id,
        "ssh_key" if "pkey" in options else "password",
        ", ".join(sorted(options)),
    )
    return storage


def read_backend_settings(row):
    """Return the settings of a storage_backend row as {field name: value}.

    The settings are in ``server_env_defaults`` (server environment) or in
    columns (storage_backend_sftp without server environment). A value of
    ``server_env_defaults`` wins over the column.
    """
    settings = {
        field_name: row[field_name]
        for field_name in SETTING_FIELDS
        if row.get(field_name) not in (None, "")
    }
    settings.update(
        {
            field_name: value
            for field_name, value in read_env_defaults(
                row.get("server_env_defaults")
            ).items()
            if value not in (None, "")
        }
    )
    return settings


def read_env_defaults(env_defaults):
    """Return the server environment defaults as {field name: value}.

    The keys are ``<field>_env_default`` or ``x_<field>_env_default``:
    accept both. When both have a value, the key without "x_" wins: the ORM
    can write a default value under the "x_" key.
    """
    if not env_defaults:
        return {}
    if isinstance(env_defaults, str):
        env_defaults = json.loads(env_defaults)
    settings = {}
    unprefixed_settings = {}
    for key, value in env_defaults.items():
        field_name = key.removesuffix("_env_default")
        if field_name.startswith("x_"):
            settings[field_name[2:]] = value
        else:
            unprefixed_settings[field_name] = value
    settings.update(
        {
            name: value
            for name, value in unprefixed_settings.items()
            if value not in (None, "")
        }
    )
    return settings


def get_sftp_options(settings):
    """Map storage_backend_sftp settings to fsspec sftp options.

    fsspec gives ``host`` and all other options to paramiko
    ``SSHClient.connect()``. The key stays as text here: JSON cannot hold a
    key object. ``fs.storage._get_fs_options()`` loads it before use.
    """
    options = {
        "host": settings.get("sftp_server"),
        "port": int(settings.get("sftp_port") or 22),
        "username": settings.get("sftp_login"),
        # Use only the saved credentials, not the SSH agent or the key files
        # of the Odoo server.
        "allow_agent": False,
        "look_for_keys": False,
    }
    if settings.get("sftp_auth_method") == "ssh_key":
        options["pkey"] = settings.get("sftp_ssh_private_key")
    else:
        options["password"] = settings.get("sftp_password")
    return options


def _get_free_code(env, name, storage_backend_id):
    base_code = env["ir.http"]._slugify(name or "").replace("-", "_") or "edi_sftp"
    code = base_code
    suffix = 0
    while env["fs.storage"].search_count([("code", "=", code)]):
        suffix += 1
        code = f"{base_code}_{storage_backend_id}" + (
            f"_{suffix}" if suffix > 1 else ""
        )
    return code


def _check_exchange_types(env, backends):
    """Make sure the exchange types of the backends can use the storage.

    A type sends with the model in ``send_model_id`` and receives with the
    model in ``receive_model_id``. An empty one gets the storage handler of
    edi_storage_oca: it sends and reads the files of the storage. Another
    handler can be on purpose, so we only report it.
    """
    storage_handler = env.ref("edi_storage_oca.model_edi_oca_storage_handler")
    for direction, field_name in (
        ("output", "send_model_id"),
        ("input", "receive_model_id"),
    ):
        exchange_types = (
            env["edi.exchange.type"]
            .with_context(active_test=False)
            .search(
                [
                    ("direction", "=", direction),
                    "|",
                    ("backend_id", "in", backends.ids),
                    "&",
                    ("backend_id", "=", False),
                    ("backend_type_id", "in", backends.backend_type_id.ids),
                ]
            )
        )
        to_fix = exchange_types.filtered(lambda t, f=field_name: not t[f])
        if to_fix:
            to_fix.write({field_name: storage_handler.id})
        other = exchange_types.filtered(lambda t, f=field_name: t[f] != storage_handler)
        _logger.info(
            "EDI storage migration: %s %s exchange types, %s set on %s",
            len(exchange_types),
            direction,
            field_name,
            to_fix.ids,
        )
        if other:
            _logger.warning(
                "EDI storage migration: %s exchange types %s have another %s, "
                "please check them",
                direction,
                other.ids,
                field_name,
            )
        if direction == "output":
            no_dir = backends.filtered(lambda b: not b.output_dir_pending)
            if exchange_types and no_dir:
                _logger.warning(
                    "EDI storage migration: EDI backends %s have no output "
                    "pending directory, files go to the root of the server",
                    no_dir.ids,
                )


def _log_pending_output_records(env, backends):
    """Report the output records that the EDI crons will send to the storage.

    Some may be old: check them before the crons run.
    """
    env.cr.execute(
        SQL(
            """
            SELECT r.edi_exchange_state, count(*)
            FROM edi_exchange_record r
            JOIN edi_exchange_type t ON t.id = r.type_id
            WHERE r.backend_id IN %s
              AND t.direction = 'output'
              AND r.edi_exchange_state IN ('new', 'output_pending')
            GROUP BY r.edi_exchange_state
            ORDER BY r.edi_exchange_state
            """,
            tuple(backends.ids),
        )
    )
    counts = dict(env.cr.fetchall())
    if counts:
        _logger.warning(
            "EDI storage migration: output records waiting to be sent on "
            "backends %s: %s. The EDI crons will send them.",
            backends.ids,
            counts,
        )
