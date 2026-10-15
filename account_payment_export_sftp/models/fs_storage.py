# Copyright 2026 Compassion CH (<https://www.compassion.ch>)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from io import StringIO

import paramiko

from odoo import models
from odoo.exceptions import UserError

KEY_CLASSES = (paramiko.RSAKey, paramiko.ECDSAKey, paramiko.Ed25519Key)


class FSStorage(models.Model):
    _inherit = "fs.storage"

    def _get_fs_options(self):
        options = super()._get_fs_options()
        pkey = options.get("pkey")
        if self.protocol == "sftp" and isinstance(pkey, str):
            # paramiko needs a key object, but the JSON options can only hold
            # the key as text.
            options = dict(options, pkey=self._load_private_key(pkey))
        return options

    def _load_private_key(self, key_text):
        """Return a paramiko key object from a private key in text form."""
        for key_class in KEY_CLASSES:
            try:
                return key_class.from_private_key(StringIO(key_text))
            except paramiko.SSHException:
                continue
        # Never put the key in the message.
        raise UserError(
            self.env._(
                "FS storage %(name)s: the private key (pkey option) cannot be read.",
                name=self.display_name,
            )
        )
