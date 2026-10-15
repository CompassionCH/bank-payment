# Copyright 2026 Compassion CH (<https://www.compassion.ch>)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import json
from io import StringIO

import paramiko

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase
from odoo.tools import SQL, file_open

from ..storage_migration import LEGACY_COLUMN, migrate_edi_storage, read_env_defaults

LOGGER = "odoo.addons.account_payment_export_sftp.storage_migration"


class TestStorageMigration(TransactionCase):
    """Fake the state left by edi_storage_oca 16.0.1.0.0 and migrate it.

    Nothing here connects to a server.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.cr.execute(
            SQL(
                "ALTER TABLE edi_backend ADD COLUMN IF NOT EXISTS %s integer",
                SQL.identifier(LEGACY_COLUMN),
            )
        )
        cls.key_text = cls._new_key_text()
        cls.storage_handler = cls.env.ref(
            "edi_storage_oca.model_edi_oca_storage_handler"
        )
        storage_backend = cls.env["storage.backend"].create({"name": "Test Bank SFTP"})
        # The ORM writes its defaults at flush: do it before the SQL below.
        storage_backend.flush_recordset()
        cls.storage_backend = storage_backend
        # Same keys as the v14 data, without the "x_" prefix of v18.
        cls._set_env_defaults(
            {
                "backend_type_env_default": "sftp",
                "sftp_auth_method_env_default": "ssh_key",
                "sftp_login_env_default": "test-user",
                "sftp_port_env_default": 2222,
                "sftp_server_env_default": "sftp.example.com",
                "sftp_ssh_private_key_env_default": cls.key_text,
            }
        )
        backend_type = cls.env["edi.backend.type"].create(
            {"name": "Test SFTP bank", "code": "test_sftp_bank"}
        )
        cls.backends = cls.env["edi.backend"].create(
            [
                {
                    "name": "Test bank export",
                    "backend_type_id": backend_type.id,
                    "output_dir_pending": "out",
                },
                {
                    "name": "Test bank import",
                    "backend_type_id": backend_type.id,
                    "output_dir_pending": "out",
                },
            ]
        )
        cls.exchange_type = cls.env["edi.exchange.type"].create(
            {
                "name": "Test SFTP out",
                "code": "test_sftp_out",
                "backend_type_id": backend_type.id,
                "direction": "output",
            }
        )
        cls.input_exchange_type = cls.env["edi.exchange.type"].create(
            {
                "name": "Test SFTP in",
                "code": "test_sftp_in",
                "backend_type_id": backend_type.id,
                "direction": "input",
            }
        )
        cls.exchange_records = cls.env["edi.exchange.record"]
        for backend in cls.backends:
            cls.exchange_records |= backend.create_record(cls.exchange_type.code, {})
        cls.env.cr.execute(
            SQL(
                "UPDATE edi_backend SET %s = %s WHERE id IN %s",
                SQL.identifier(LEGACY_COLUMN),
                storage_backend.id,
                tuple(cls.backends.ids),
            )
        )
        cls.env["edi.backend"].invalidate_model()

    @classmethod
    def _set_env_defaults(cls, env_defaults):
        cls.env.cr.execute(
            SQL(
                "UPDATE storage_backend SET server_env_defaults = %s WHERE id = %s",
                json.dumps(env_defaults),
                cls.storage_backend.id,
            )
        )

    @staticmethod
    def _new_key_text():
        key_buffer = StringIO()
        paramiko.RSAKey.generate(1024).write_private_key(key_buffer)
        return key_buffer.getvalue()

    def _new_storages(self):
        return self.env["fs.storage"].search([("name", "=", "Test Bank SFTP")])

    def _migrate(self):
        """Run the migration, return its log, read everything again from the DB."""
        with self.assertLogs(LOGGER, level="INFO") as logs:
            migrate_edi_storage(self.env)
        self.env.invalidate_all()
        return "\n".join(logs.output)

    def test_migrate_creates_and_links_storage(self):
        self._migrate()

        storage = self._new_storages()
        self.assertEqual(len(storage), 1)
        self.assertEqual(storage.protocol, "sftp")
        self.assertEqual(storage.check_connection_method, "ls")
        self.assertEqual(
            storage.json_options,
            {
                "host": "sftp.example.com",
                "port": 2222,
                "username": "test-user",
                "allow_agent": False,
                "look_for_keys": False,
                "pkey": self.key_text,
            },
        )
        self.assertEqual(self.backends.storage_id, storage)
        self.assertEqual(self.exchange_records.storage_id, storage)
        self.assertEqual(self.exchange_type.send_model_id, self.storage_handler)
        self.assertEqual(
            self.input_exchange_type.receive_model_id, self.storage_handler
        )

    def test_migrate_logs_no_credentials(self):
        log = self._migrate()
        for secret in (
            "sftp.example.com",
            "test-user",
            "PRIVATE KEY",
            self.key_text.splitlines()[1],
        ):
            self.assertNotIn(secret, log)
        # The new records can now be sent: the log must say so.
        self.assertIn("output records waiting to be sent", log)

    def test_migrate_twice_does_nothing(self):
        self._migrate()
        storage = self._new_storages()
        log = self._migrate()
        self.assertIn("all EDI backends have a storage", log)
        self.assertEqual(self._new_storages(), storage)
        self.assertEqual(self.backends.storage_id, storage)

    def test_migrate_reuses_storage_of_linked_backend(self):
        self._migrate()
        storage = self._new_storages()
        self.backends[1].storage_id = False
        self._migrate()
        self.assertEqual(self._new_storages(), storage)
        self.assertEqual(self.backends[1].storage_id, storage)

    def test_migrate_keeps_other_send_handler(self):
        # Any other model works here: the migration must keep it as it is.
        other_handler = self.env["ir.model"]._get("edi.exchange.type")
        self.exchange_type.send_model_id = other_handler
        log = self._migrate()
        self.assertEqual(self.exchange_type.send_model_id, other_handler)
        self.assertIn("have another send_model_id", log)

    def test_migrate_password_auth(self):
        self._set_env_defaults(
            {
                "x_backend_type_env_default": "sftp",
                "x_sftp_auth_method_env_default": "pwd",
                "x_sftp_login_env_default": "test-user",
                "x_sftp_password_env_default": "test-password",
                "x_sftp_server_env_default": "sftp.example.com",
            }
        )
        log = self._migrate()
        self.assertNotIn("test-password", log)
        options = self._new_storages().json_options
        self.assertEqual(options["password"], "test-password")
        self.assertEqual(options["port"], 22)
        self.assertNotIn("pkey", options)

    def test_migrate_skips_other_backend_type(self):
        self._set_env_defaults({"backend_type_env_default": "filesystem"})
        log = self._migrate()
        self.assertIn("only sftp is migrated", log)
        self.assertFalse(self._new_storages())
        self.assertFalse(self.backends.storage_id)

    def test_migrate_finds_free_code(self):
        taken = self.env["fs.storage"].create(
            [
                {"name": "Taken", "code": code, "protocol": "memory"}
                for code in (
                    "test_bank_sftp",
                    f"test_bank_sftp_{self.storage_backend.id}",
                )
            ]
        )
        self._migrate()
        self.assertEqual(
            self._new_storages().code,
            f"test_bank_sftp_{self.storage_backend.id}_2",
        )
        self.assertNotIn(self._new_storages(), taken)

    def test_neutralize_unlinks_storage(self):
        self._migrate()
        self.assertTrue(self.backends.storage_id)
        with file_open("account_payment_export_sftp/data/neutralize.sql") as file:
            self.env.cr.execute(file.read())
        self.env.invalidate_all()
        self.assertFalse(self.backends.storage_id)

    def test_fs_options_load_key(self):
        self._migrate()
        options = self._new_storages()._get_fs_options()
        self.assertIsInstance(options["pkey"], paramiko.PKey)
        # The saved options keep the key as text.
        self.assertEqual(self._new_storages().json_options["pkey"], self.key_text)

    def test_fs_options_bad_key(self):
        self._migrate()
        storage = self._new_storages()
        storage.options = json.dumps(dict(storage.json_options, pkey="not a key"))
        with self.assertRaises(UserError) as error:
            storage._get_fs_options()
        self.assertNotIn("not a key", str(error.exception))

    def test_read_env_defaults(self):
        self.assertEqual(
            read_env_defaults('{"sftp_port_env_default": 22}'), {"sftp_port": 22}
        )
        self.assertEqual(
            read_env_defaults({"x_sftp_port_env_default": 22}), {"sftp_port": 22}
        )
        self.assertEqual(read_env_defaults(None), {})
        # The v14 value wins over a v18 default, unless it is empty.
        self.assertEqual(
            read_env_defaults(
                {
                    "x_backend_type_env_default": "filesystem",
                    "backend_type_env_default": "sftp",
                    "x_sftp_port_env_default": 2222,
                    "sftp_port_env_default": None,
                }
            ),
            {"backend_type": "sftp", "sftp_port": 2222},
        )
