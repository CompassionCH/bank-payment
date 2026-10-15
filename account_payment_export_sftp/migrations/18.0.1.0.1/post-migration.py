# Copyright 2026 Compassion CH (<https://www.compassion.ch>)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import SUPERUSER_ID, api

from odoo.addons.account_payment_export_sftp.storage_migration import (
    migrate_edi_storage,
)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    migrate_edi_storage(env)
