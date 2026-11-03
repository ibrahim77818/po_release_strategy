# -*- coding: utf-8 -*-
from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import float_compare


class PoReleaseStrategy(models.Model):
    _name = 'po.release.strategy'
    _description = 'Purchase Order Release Strategy'
    _inherit = ['mail.thread']
    _order = 'sequence, id'
    _check_company_auto = True

    name = fields.Char(required=True, translate=True, tracking=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True, tracking=True)
    company_id = fields.Many2one(
        'res.company', required=True, index=True,
        default=lambda self: self.env.company)
    currency_id = fields.Many2one(
        related='company_id.currency_id', string='Company Currency')
    description = fields.Text(translate=True)
    is_default = fields.Boolean(
        string='Default Strategy', compute='_compute_is_default')

    amount_basis = fields.Selection(
        [('total', 'Total (taxes included)'),
         ('untaxed', 'Untaxed Amount')],
        string='Amount Basis', required=True, default='total', tracking=True,
        help='Order amount compared with the rule thresholds, converted to '
             'the company currency.')
    post_approval_action = fields.Selection(
        [('auto_confirm', 'Confirm the order automatically'),
         ('notify_buyer', 'Notify the buyer to confirm manually')],
        string='After Final Approval', required=True, default='auto_confirm',
        tracking=True)
    approval_delay = fields.Integer(
        string='Approval Deadline (Days)', default=1,
        help='Number of days given to approvers to act on their activity.')
    locked_field_ids = fields.Many2many(
        'ir.model.fields', 'po_release_strategy_locked_field_rel',
        'strategy_id', 'field_id', string='Locked Fields',
        domain="[('id', 'in', lockable_field_ids)]",
        help='Purchase order fields frozen by the release strategy. Changing '
             'any of them after submission resets the strategy, clears the '
             'approvals and notifies all approvers again.')
    lockable_field_ids = fields.Many2many(
        'ir.model.fields', compute='_compute_lockable_field_ids')
    lock_confirmed_policy = fields.Selection(
        [('reset', 'Reset the release strategy'),
         ('block', 'Block the modification')],
        string='Changes on Confirmed Orders', required=True, default='reset',
        help='What happens when a locked field is modified on an order that '
             'is already confirmed.')
    rule_ids = fields.One2many(
        'po.release.strategy.rule', 'strategy_id', string='Approval Rules',
        copy=True)
    rule_count = fields.Integer(compute='_compute_counts')
    order_count = fields.Integer(compute='_compute_counts')

    def _compute_is_default(self):
        for strategy in self:
            strategy.is_default = (
                strategy.company_id.po_release_default_strategy_id == strategy)

    def _compute_lockable_field_ids(self):
        names = self.env['purchase.order']._po_release_lockable_field_names()
        lockable = self.env['ir.model.fields'].sudo().search([
            ('model', '=', 'purchase.order'), ('name', 'in', names)])
        for strategy in self:
            strategy.lockable_field_ids = lockable

    def _compute_counts(self):
        order_data = self.env['purchase.order'].read_group(
            [('release_strategy_id', 'in', self.ids)],
            ['release_strategy_id'], ['release_strategy_id'])
        orders = {d['release_strategy_id'][0]: d['release_strategy_id_count']
                  for d in order_data}
        for strategy in self:
            strategy.rule_count = len(strategy.rule_ids)
            strategy.order_count = orders.get(strategy.id, 0)

    @api.constrains('locked_field_ids')
    def _check_locked_fields(self):
        allowed = set(self.env['purchase.order']._po_release_lockable_field_names())
        for strategy in self:
            invalid = strategy.locked_field_ids.filtered(
                lambda f: f.model != 'purchase.order' or f.name not in allowed)
            if invalid:
                raise ValidationError(_(
                    'The following fields cannot be locked by a release '
                    'strategy: %s', ', '.join(invalid.mapped('field_description'))))

    @api.constrains('approval_delay')
    def _check_approval_delay(self):
        if any(s.approval_delay < 0 for s in self):
            raise ValidationError(_('The approval deadline cannot be negative.'))

    def copy(self, default=None):
        default = dict(default or {})
        default.setdefault('name', _('%s (copy)', self.name))
        return super().copy(default)

    def action_set_default(self):
        self.ensure_one()
        self.company_id.po_release_default_strategy_id = self
        return True

    def action_view_orders(self):
        self.ensure_one()
        action = self.env['ir.actions.actions']._for_xml_id('purchase.purchase_rfq')
        action['domain'] = [('release_strategy_id', '=', self.id)]
        action['context'] = {'create': False}
        return action

    def _get_applicable_rules(self, amount):
        """Rules whose amount range contains ``amount`` (company currency)."""
        self.ensure_one()
        return self.rule_ids.filtered(lambda r: r._matches_amount(amount)).sorted(
            lambda r: (r.sequence, r.id))


class PoReleaseStrategyRule(models.Model):
    _name = 'po.release.strategy.rule'
    _description = 'Purchase Order Release Strategy Rule'
    _order = 'strategy_id, sequence, id'

    strategy_id = fields.Many2one(
        'po.release.strategy', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='strategy_id.company_id', store=True)
    currency_id = fields.Many2one(related='strategy_id.currency_id')
    sequence = fields.Integer(
        default=10,
        help='Approval stage. Rules sharing the same sequence are approved in '
             'parallel; the next stage starts once the current one is done.')
    role = fields.Char(required=True, help='Function of the approver, e.g. Finance Manager.')
    approver_type = fields.Selection(
        [('user', 'Specific User'), ('group', 'User Group')],
        string='Approver Type', required=True, default='user')
    user_id = fields.Many2one(
        'res.users', string='Approver', domain="[('share', '=', False)]")
    group_id = fields.Many2one('res.groups', string='Approver Group')
    min_amount = fields.Monetary(
        string='Minimum Amount', currency_field='currency_id', default=0.0)
    max_amount = fields.Monetary(
        string='Maximum Amount', currency_field='currency_id', default=0.0,
        help='Leave 0 for no upper limit.')

    @api.onchange('approver_type')
    def _onchange_approver_type(self):
        if self.approver_type == 'user':
            self.group_id = False
        else:
            self.user_id = False

    @api.constrains('approver_type', 'user_id', 'group_id')
    def _check_approver(self):
        for rule in self:
            if rule.approver_type == 'user' and not rule.user_id:
                raise ValidationError(_('Rule "%s": select the approver user.', rule.role))
            if rule.approver_type == 'group' and not rule.group_id:
                raise ValidationError(_('Rule "%s": select the approver group.', rule.role))

    @api.constrains('min_amount', 'max_amount')
    def _check_amounts(self):
        for rule in self:
            if rule.min_amount < 0 or rule.max_amount < 0:
                raise ValidationError(_('Rule "%s": amounts cannot be negative.', rule.role))
            if rule.max_amount and rule.max_amount < rule.min_amount:
                raise ValidationError(_(
                    'Rule "%s": the maximum amount must be greater than the '
                    'minimum amount.', rule.role))

    def _matches_amount(self, amount):
        self.ensure_one()
        rounding = self.currency_id.rounding or 0.01
        if float_compare(amount, self.min_amount, precision_rounding=rounding) < 0:
            return False
        if self.max_amount and float_compare(
                amount, self.max_amount, precision_rounding=rounding) > 0:
            return False
        return True

    def _get_approver_display(self):
        self.ensure_one()
        if self.approver_type == 'user':
            return self.user_id.name or ''
        return self.group_id.full_name or self.group_id.name or ''
