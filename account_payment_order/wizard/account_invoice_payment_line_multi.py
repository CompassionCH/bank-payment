# © 2016 Akretion (<https://www.akretion.com>)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).

from odoo import models, api


class AccountInvoicePaymentLineMulti(models.TransientModel):
    _name = 'account.invoice.payment.line.multi'
    _description = 'Create payment lines from invoice tree view'

    @api.multi
    def run(self):
        self.ensure_one()
        assert self._context['active_model'] == 'account.invoice',\
            'Active model should be account.invoice'
        invoices = self.env['account.invoice'].browse(
            self._context['active_ids'])
        action = invoices.create_account_payment_line()

        order = self.env[action['res_model']].browse(action['res_id'])
        local_instrument = ""
        if order.company_partner_bank_id.acc_type == "iban":
            local_instrument = "LSV+"
        elif order.company_partner_bank_id.acc_type == "postal":
            local_instrument = "DDCOR1"
        order.payment_line_ids.write({
            "local_instrument": local_instrument
        })
        return action
