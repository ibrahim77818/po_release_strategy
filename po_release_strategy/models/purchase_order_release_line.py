# -*- coding: utf-8 -*-
from datetime import timedelta

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import format_amount

from .mail_activity import RELEASE_CTX

LINE_STATES = [
    ('waiting', 'Waiting'),
    ('pending', 'Pending Approval'),
    ('approved', 'Approved'),
    ('bypassed', 'Bypassed'),
    ('rejected', 'Rejected'),
    ('cancelled', 'Cancelled'),
]


class PurchaseOrderReleaseLine(models.Model):
    _name = 'purchase.order.release.line'
    _description = 'Purchase Order Release Approval'
    _order = 'order_id, sequence, id'

    order_id = fields.Many2one(
        'purchase.order', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='order_id.company_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    rule_id = fields.Many2one('po.release.strategy.rule', ondelete='set null')
    sequence = fields.Integer(readonly=True)
    role = fields.Char(readonly=True)
    approver_type = fields.Selection(
        [('user', 'Specific User'), ('group', 'User Group')],
        string='Approver Type', readonly=True)
    user_id = fields.Many2one('res.users', string='Approver', readonly=True)
    group_id = fields.Many2one('res.groups', string='Approver Group', readonly=True)
    approver_display = fields.Char(string='Approver(s)', readonly=True)
    min_amount = fields.Monetary(currency_field='currency_id', readonly=True)
    max_amount = fields.Monetary(currency_field='currency_id', readonly=True)
    state = fields.Selection(LINE_STATES, default='waiting', required=True,
                             readonly=True, index=True)
    assigned_date = fields.Datetime(string='Assigned On', readonly=True)
    approval_date = fields.Datetime(string='Approved On', readonly=True)
    approved_by_id = fields.Many2one('res.users', string='Approved By', readonly=True)
    rejected_date = fields.Datetime(string='Rejected On', readonly=True)
    rejected_by_id = fields.Many2one('res.users', string='Rejected By', readonly=True)
    notes = fields.Text(string='Approver Notes', readonly=True)
    activity_ids = fields.One2many('mail.activity', 'po_release_line_id', readonly=True)
    is_current_user_approver = fields.Boolean(compute='_compute_is_current_user_approver')

    @api.depends_context('uid')
    @api.depends('state', 'user_id', 'group_id')
    def _compute_is_current_user_approver(self):
        user = self.env.user
        for line in self:
            line.is_current_user_approver = (
                line.state == 'pending' and user in line._get_approver_users())

    # ------------------------------------------------------------------
    # Approvers
    # ------------------------------------------------------------------
    def _get_approver_users(self):
        """Internal, active users allowed to act on this line."""
        self.ensure_one()
        if self.approver_type == 'user':
            users = self.user_id
        else:
            users = self.group_id.sudo().users
        company = self.order_id.company_id
        return users.filtered(
            lambda u: u.active and not u.share and company in u.company_ids)

    def _get_notifiable_users(self):
        """Approvers who can actually open the purchase order."""
        self.ensure_one()
        order = self.order_id
        allowed = self.env['res.users']
        for user in self._get_approver_users():
            model = self.env['purchase.order'].with_user(user)
            if not model.check_access_rights('read', raise_exception=False):
                continue
            if not order.with_user(user)._filter_access_rules('read'):
                continue
            allowed |= user
        return allowed

    # ------------------------------------------------------------------
    # Life cycle
    # ------------------------------------------------------------------
    def _activate(self):
        """Move waiting lines to pending and schedule the approval activities."""
        now = fields.Datetime.now()
        for line in self:
            users = line._get_notifiable_users()
            if not users:
                raise UserError(_(
                    'Release rule "%(role)s": no active approver with access to '
                    'purchase orders of %(company)s was found (%(approver)s).',
                    role=line.role, company=line.order_id.company_id.name,
                    approver=line.approver_display))
            line.write({'state': 'pending', 'assigned_date': now})
            order = line.order_id.sudo().with_context(**{RELEASE_CTX: True})
            deadline = fields.Date.context_today(line) + timedelta(
                days=order.release_strategy_id.approval_delay or 0)
            note = Markup('<p>%s</p>') % _(
                'Please review purchase order %(order)s (%(amount)s) as %(role)s.',
                order=order.name, role=line.role,
                amount=format_amount(self.env, order.release_amount_company,
                                     order.release_currency_id))
            for user in users:
                order.activity_schedule(
                    'po_release_strategy.mail_activity_type_po_release',
                    date_deadline=deadline,
                    summary=_('Release approval: %s', line.role),
                    note=note,
                    user_id=user.id,
                    po_release_line_id=line.id,
                )

    def _close_activities(self, done_by=None, feedback=False):
        """Mark the activity of ``done_by`` as done and drop the others."""
        for line in self:
            activities = line.activity_ids.sudo().with_context(**{RELEASE_CTX: True})
            mine = activities.filtered(lambda a: done_by and a.user_id == done_by)
            if mine:
                mine.action_feedback(feedback=feedback or False)
            (activities - mine).exists().unlink()

    def _mark_approved(self, user, notes=False, bypass=False):
        self._close_activities(done_by=None if bypass else user, feedback=notes)
        self.write({
            'state': 'bypassed' if bypass else 'approved',
            'approval_date': fields.Datetime.now(),
            'approved_by_id': user.id,
            'notes': notes or False,
        })

    def _mark_rejected(self, user, notes):
        self._close_activities(done_by=user, feedback=notes)
        self.write({
            'state': 'rejected',
            'rejected_date': fields.Datetime.now(),
            'rejected_by_id': user.id,
            'notes': notes,
        })

    def _cancel(self):
        self._close_activities()
        self.filtered(lambda l: l.state in ('waiting', 'pending')).write({'state': 'cancelled'})

    def unlink(self):
        self.activity_ids.sudo().with_context(**{RELEASE_CTX: True}).unlink()
        return super().unlink()
