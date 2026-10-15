# Copyright 2026 Compassion CH (<https://www.compassion.ch>)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import json
from io import StringIO

import paramiko

from odoo.tests.common import TransactionCase

from ..storage_migration import LEGACY_COLUMN, migrate_edi_storage, read_env_defaults


class TestStorageMigration(TransactionCase):
    """Fake the state left by edi_storage_oca 16.0.1.0.0 and migrate it.

    Nothing here connects to a server.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.cr.execute(
            f"ALTER TABLE edi_backend ADD COLUMN IF NOT EXISTS {LEGACY_COLUMN} integer"
        )
        cls.key_text = cls._new_key_text()
        storage_backend = cls.env["storage.backend"].create({"name": "Test Bank SFTP"})
        # The ORM writes its defaults at flush: do it before the SQL below.
        storage_backend.flush_recordset()
        # Same keys as the v14 data, without the "x_" prefix of v18.
        cls.env.cr.execute(
            "UPDATE storage_backend SET server_env_defaults = %s WHERE id = %s",
            (
                json.dumps(
                    {
                        "backend_type_env_default": "sftp",
                        "sftp_auth_method_env_default": "ssh_key",
                        "sftp_login_env_default": "test-user",
                        "sftp_port_env_default": 2222,
                        "sftp_server_env_default": "sftp.example.com",
                        "sftp_ssh_private_key_env_default": cls.key_text,
                    }
                ),
                storage_backend.id,
            ),
        )
        cls.storage_backend = storage_backend
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
        cls.exchange_records = cls.env["edi.exchange.record"]
        for backend in cls.backends:
            cls.exchange_records |= backend.create_record(cls.exchange_type.code, {})
        cls.env.cr.execute(
            f"UPDATE edi_backend SET {LEGACY_COLUMN} = %s WHERE id IN %s",
            (storage_backend.id, tuple(cls.backends.ids)),
        )
        cls.env["edi.backend"].invalidate_model()

    @staticmethod
    def _new_key_text():
        key_buffer = StringIO()
        paramiko.RSAKey.generate(1024).write_private_key(key_buffer)
        return key_buffer.getvalue()

    def _new_storages(self):
        return self.env["fs.storage"].search([("name", "=", "Test Bank SFTP")])

    def test_migrate_creates_and_links_storage(self):
        migrate_edi_storage(self.env)

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
        self.assertEqual(
            self.exchange_type.send_model_id,
            self.env.ref("edi_storage_oca.model_edi_oca_storage_handler"),
        )

    def test_migrate_twice_does_nothing(self):
        migrate_edi_storage(self.env)
        storage = self._new_storages()
        migrate_edi_storage(self.env)
        self.assertEqual(self._new_storages(), storage)
        self.assertEqual(self.backends.storage_id, storage)

    def test_migrate_reuses_storage_of_linked_backend(self):
        migrate_edi_storage(self.env)
        storage = self._new_storages()
        self.backends[1].storage_id = False
        migrate_edi_storage(self.env)
        self.assertEqual(self._new_storages(), storage)
        self.assertEqual(self.backends[1].storage_id, storage)

    def test_migrate_password_auth(self):
        self.env.cr.execute(
            "UPDATE storage_backend SET server_env_defaults = %s WHERE id = %s",
            (
                json.dumps(
                    {
                        "x_backend_type_env_default": "sftp",
                        "x_sftp_auth_method_env_default": "pwd",
                        "x_sftp_login_env_default": "test-user",
                        "x_sftp_password_env_default": "test-password",
                        "x_sftp_server_env_default": "sftp.example.com",
                    }
                ),
                self.storage_backend.id,
            ),
        )
        migrate_edi_storage(self.env)
        options = self._new_storages().json_options
        self.assertEqual(options["password"], "test-password")
        self.assertEqual(options["port"], 22)
        self.assertNotIn("pkey", options)

    def test_fs_options_load_key(self):
        migrate_edi_storage(self.env)
        options = self._new_storages()._get_fs_options()
        self.assertIsInstance(options["pkey"], paramiko.PKey)
        # The saved options keep the key as text.
        self.assertEqual(self._new_storages().json_options["pkey"], self.key_text)

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
