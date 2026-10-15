# Copyright 2026 Compassion CH (<https://www.compassion.ch>)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Create the SFTP fs.storage that the 16.0 migration of edi_storage_oca missed.

In edi_storage_oca 16.0.1.0.0, pre-migrate renames ``edi_backend.storage_id``
(a storage.backend) to a legacy column. Then post-migrate reads the new,
empty ``storage_id`` column, so it creates no fs.storage and links nothing.
The settings are still in ``storage_backend.server_env_defaults``.

This code creates the fs.storage from these settings and links the EDI
backends and their exchange records to it. It never connects to the server.
It never logs a setting value: settings hold credentials.
"""

import json
import logging

from odoo.tools.sql import column_exists, table_exists

_logger = logging.getLogger(__name__)

LEGACY_COLUMN = "openupgrade_legacy_16_0_storage_id"


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
        f"""
        SELECT {LEGACY_COLUMN}, array_agg(id ORDER BY id)
        FROM edi_backend
        WHERE storage_id IS NULL AND {LEGACY_COLUMN} IS NOT NULL
        GROUP BY {LEGACY_COLUMN}
        ORDER BY {LEGACY_COLUMN}
        """
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
        # Same as edi_storage_oca 16.0.1.0.2: each record gets its backend storage.
        cr.execute(
            """
            UPDATE edi_exchange_record
            SET storage_id = %s
            WHERE backend_id IN %s AND storage_id IS NULL
            """,
            (storage.id, tuple(backend_ids)),
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
        _check_output_exchange_types(env, backends)


def _get_linked_storage(env, storage_backend_id):
    """Return the fs.storage already used for the same storage.backend, if any.

    This happens when only some backends were linked before.
    """
    env.cr.execute(
        f"""
        SELECT storage_id FROM edi_backend
        WHERE {LEGACY_COLUMN} = %s AND storage_id IS NOT NULL
        LIMIT 1
        """,
        (storage_backend_id,),
    )
    row = env.cr.fetchone()
    return env["fs.storage"].browse(row[0]) if row else env["fs.storage"]


def _create_sftp_storage(env, storage_backend_id):
    env.cr.execute(
        "SELECT name, server_env_defaults FROM storage_backend WHERE id = %s",
        (storage_backend_id,),
    )
    row = env.cr.fetchone()
    if not row:
        _logger.warning(
            "EDI storage migration: storage.backend %s not found", storage_backend_id
        )
        return env["fs.storage"]
    name, env_defaults = row
    settings = read_env_defaults(env_defaults)
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
            # The default "marker_file" writes a file at the root of the server
            # each time the storage is used. A bank server may refuse it, so we
            # only list a file, like v14 which never wrote there.
            "check_connection_method": "ls",
        }
    )
    env.registry.clear_cache()
    _logger.info(
        "EDI storage migration: fs.storage %s created (sftp, auth %s, options %s)",
        storage.id,
        "ssh_key" if "pkey" in options else "password",
        ", ".join(sorted(options)),
    )
    return storage


def read_env_defaults(env_defaults):
    """Return the server environment defaults as {field name: value}.

    v14 saved them as ``<field>_env_default``, v18 reads
    ``x_<field>_env_default``: accept both. When both are set, keep the v14
    value: it is the migrated data, the v18 one can be a default only.
    """
    if not env_defaults:
        return {}
    if isinstance(env_defaults, str):
        env_defaults = json.loads(env_defaults)
    settings = {}
    v14_settings = {}
    for key, value in env_defaults.items():
        field_name = key.removesuffix("_env_default")
        if field_name.startswith("x_"):
            settings[field_name[2:]] = value
        else:
            v14_settings[field_name] = value
    settings.update(
        {name: value for name, value in v14_settings.items() if value not in (None, "")}
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
        # Like v14: use only the saved credentials, not the SSH agent or the
        # key files of the Odoo server.
        "allow_agent": False,
        "look_for_keys": False,
    }
    if settings.get("sftp_auth_method") == "ssh_key":
        options["pkey"] = settings.get("sftp_ssh_private_key")
    else:
        options["password"] = settings.get("sftp_password")
    return options


def _get_free_code(env, name, storage_backend_id):
    code = env["ir.http"]._slugify(name or "").replace("-", "_") or "edi_sftp"
    if env["fs.storage"].search_count([("code", "=", code)]):
        code = f"{code}_{storage_backend_id}"
    return code


def _check_output_exchange_types(env, backends):
    """Make sure the output exchange types can send files to the storage.

    In 18, a type sends with the model in ``send_model_id``. edi_oca puts the
    component handler where it is empty, but no module has a send component
    for these types: only the storage handler of edi_storage_oca can send.
    """
    storage_handler = env.ref("edi_storage_oca.model_edi_oca_storage_handler")
    component_handler = env.ref(
        "edi_component_oca.model_edi_oca_component_handler", raise_if_not_found=False
    )
    exchange_types = (
        env["edi.exchange.type"]
        .with_context(active_test=False)
        .search(
            [
                ("direction", "=", "output"),
                "|",
                ("backend_id", "in", backends.ids),
                "&",
                ("backend_id", "=", False),
                ("backend_type_id", "in", backends.backend_type_id.ids),
            ]
        )
    )
    to_fix = exchange_types.filtered(
        lambda t: not t.send_model_id or t.send_model_id == component_handler
    )
    if to_fix:
        to_fix.write({"send_model_id": storage_handler.id})
    other = exchange_types - to_fix
    other = other.filtered(lambda t: t.send_model_id != storage_handler)
    _logger.info(
        "EDI storage migration: %s output exchange types, send handler set on %s",
        len(exchange_types),
        to_fix.ids,
    )
    if other:
        _logger.warning(
            "EDI storage migration: output exchange types %s use another send "
            "handler, please check them",
            other.ids,
        )
    no_dir = backends.filtered(lambda b: not b.output_dir_pending)
    if exchange_types and no_dir:
        _logger.warning(
            "EDI storage migration: EDI backends %s have no output pending "
            "directory, files go to the root of the server",
            no_dir.ids,
        )
