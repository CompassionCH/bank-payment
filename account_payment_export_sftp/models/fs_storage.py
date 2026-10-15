# Copyright 2026 Compassion CH (<https://www.compassion.ch>)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from io import StringIO

from odoo import models

from odoo.addons.storage_backend_sftp.components.sftp_adapter import load_ssh_key


class FSStorage(models.Model):
    _inherit = "fs.storage"

    def _get_fs_options(self):
        options = super()._get_fs_options()
        pkey = options.get("pkey")
        if self.protocol == "sftp" and isinstance(pkey, str):
            # paramiko needs a key object, but the JSON options can only hold
            # the key as text.
            options = dict(options, pkey=load_ssh_key(StringIO(pkey)))
        return options
