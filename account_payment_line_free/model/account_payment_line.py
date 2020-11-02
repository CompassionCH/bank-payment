# -*- coding: utf-8 -*-
# Copyright 2020 Compassion Suisse (http://www.compassion.ch)
# @author: David Wulliamoz, Emanuel Cino
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import models, fields


class AccountPaymentLine(models.Model):
    _inherit = 'account.payment.line'

    def free_line(self):
        """
        Set move_line_id to Null in order to cancel the related invoice
        check if the payment_line is returned, if not, check the related 
        move_line is not reconciled 
        """
        all_account_move_lines = self.mapped('move_line_id')
        full_reconcile_ids = all_account_move_lines.mapped(
            'full_reconcile_id.id')
        payment_orders = self.mapped('order_id')
        if not full_reconcile_ids:
            self.move_line_id = False
            self.payment_line_returned = True
            self._post_free_message()

        else:
            #throw an error



    def _post_free_message(self):
        """
        post message on the invoice that have been freed from the payment order
        post message on the payment order for each payment_line unlinked from the move_line.
        """
        for payment_line in self:

            # Create a link to the invoice that was removed
            invoice = payment_line.move_line_id.invoice_id
            order = payment_line.order_id
            invoice_url = u'<a href="web#id={}&view_type=form&model=' \
                u'account.invoice">{}</a>'.format(invoice.id,
                                                  invoice.move_name)
            payment_order_url = u'<a href="web#id={}&view_type=form&model=' \
                u'account.payment.order">{}</a>'.format(order.id, order.name)
            # Add a message to the invoice
            invoice.message_post(
                _(u"The invoice has been marked as returned and freed from ") + u"{}, {}"
                .format(payment_order_url, cancel_reason)
            )
            # Add a message to the payment order
            payment_line.order_id.message_post(
                invoice_url + _(u" has been unlinked from the line: ") + payment_line.name)
