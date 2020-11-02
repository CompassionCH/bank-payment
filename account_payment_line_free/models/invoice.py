# -*- coding: utf-8 -*-
# Copyright 2020 Compassion Suisse (http://www.compassion.ch)
# @author: David Wulliamoz, Emanuel Cino
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
from odoo import models, api, _, exceptions


class AccountInvoice(models.Model):

    """ add invoice freeing functionality. 
    """
    _inherit = 'account.invoice'

    @api.multi
    def free_payment_lines(self):
        """ finds related payment lines and free them.
        """
        mov_line_obj = self.env['account.move.line']
        pay_line_obj = self.env['account.payment.line']
        move_line_ids = mov_line_obj.search([('move_id', 'in', self.move_id)]).ids
        payment_lines = pay_line_obj.search([
            ('move_line_id', 'in', move_line_ids)
        ])
        if not payment_lines:
            raise exceptions.UserError(_('No payment line found !'))

        payment_lines.free_line()
