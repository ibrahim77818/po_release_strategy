# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class PoReleaseActionWizard(models.TransientModel):
    _name = 'po.release.action.wizard'
    _description = 'Purchase Release Action'

    order_id = fields.Many2one('purchase.order', required=True, readonly=True)
    action = fields.Selection(
        [('approve', 'Approve'), ('reject', 'Reject'),
         ('bypass', 'Bypass'), ('reset', 'Reset')],
        required=True, readonly=True)
    notes = fields.Text(string='Notes')
    notes_required = fields.Boolean(compute='_compute_notes_required')
    release_amount_company = fields.Monetary(
        related='order_id.release_amount_company', currency_field='currency_id')
    currency_id = fields.Many2one(related='order_id.release_currency_id')
    role_names = fields.Char(compute='_compute_role_names', string='Your Role(s)')

    @api.depends('action')
    def _compute_notes_required(self):
        for wizard in self:
            wizard.notes_required = wizard.action in ('reject', 'bypass')

    @api.depends('order_id', 'action')
    def _compute_role_names(self):
        for wizard in self:
            lines = wizard.order_id._po_release_lines_for_user(self.env.user) \
                if wizard.order_id and wizard.action in ('approve', 'reject') else False
            wizard.role_names = ', '.join(lines.mapped('role')) if lines else False

    def action_confirm(self):
        self.ensure_one()
        notes = (self.notes or '').strip()
        if self.notes_required and not notes:
            raise UserError(_('Please fill in the notes.'))
        order = self.order_id
        if self.action == 'approve':
            order._po_release_approve(notes or False)
        elif self.action == 'reject':
            order._po_release_reject(notes)
        elif self.action == 'bypass':
            order._po_release_bypass(notes)
        else:
            order._po_release_manual_reset(notes or False)
        return {'type': 'ir.actions.act_window_close'}
