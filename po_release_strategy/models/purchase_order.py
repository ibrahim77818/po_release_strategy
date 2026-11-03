# -*- coding: utf-8 -*-
import json

from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import float_round

from .mail_activity import RELEASE_CTX

# Context keys
SKIP_CHECK_CTX = 'po_release_skip_lock_check'
DEFER_CHECK_CTX = 'po_release_defer_lock_check'

RELEASE_STATES = [
    ('draft', 'Not Submitted'),
    ('pending', 'Waiting Approval'),
    ('approved', 'Released'),
    ('rejected', 'Rejected'),
]

# purchase.order fields that can never be locked: workflow, technical and
# values that legitimately evolve after confirmation.
NON_LOCKABLE_FIELDS = {
    'id', 'display_name', 'state', 'name', 'access_token', 'access_url',
    'create_uid', 'create_date', 'write_uid', 'write_date', '__last_update',
    'company_id', 'currency_rate', 'date_approve', 'date_calendar_start',
    'invoice_status', 'invoice_count', 'invoice_ids', 'receipt_status',
    'effective_date', 'mail_reminder_confirmed', 'mail_reception_confirmed',
    'group_id', 'picking_ids', 'priority', 'is_shipped', 'tax_totals',
    'country_code', 'tax_country_id', 'amount_total_cc', 'user_id',
}
# Computed totals that are still meaningful to freeze.
LOCKABLE_COMPUTED_FIELDS = {'amount_untaxed', 'amount_tax', 'amount_total'}


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    release_required = fields.Boolean(
        compute='_compute_release_required',
        help='Technical: the company enforces purchase release strategies.')
    release_strategy_id = fields.Many2one(
        'po.release.strategy', string='Release Strategy', ondelete='restrict',
        tracking=True, check_company=True,
        default=lambda self: self._po_release_default_strategy(self.env.company),
        domain="[('company_id', '=', company_id)]")
    release_state = fields.Selection(
        RELEASE_STATES, string='Release Status', default='draft', required=True,
        copy=False, tracking=True, index=True)
    release_line_ids = fields.One2many(
        'purchase.order.release.line', 'order_id', string='Release Approvals',
        copy=False)
    release_currency_id = fields.Many2one(
        related='company_id.currency_id', string='Company Currency')
    release_amount_company = fields.Monetary(
        string='Release Amount', currency_field='release_currency_id',
        copy=False, readonly=True,
        help='Order amount converted to the company currency when the order '
             'was submitted; used to select the approval rules.')
    release_submit_date = fields.Datetime(
        string='Submitted On', copy=False, readonly=True)
    release_approval_date = fields.Datetime(
        string='Released On', copy=False, readonly=True)
    release_bypassed = fields.Boolean(
        string='Approval Bypassed', copy=False, readonly=True)
    release_bypass_user_id = fields.Many2one(
        'res.users', string='Bypassed By', copy=False, readonly=True)
    release_bypass_reason = fields.Text(
        string='Bypass Reason', copy=False, readonly=True)
    release_reset_count = fields.Integer(
        string='Release Resets', copy=False, readonly=True)
    release_snapshot = fields.Text(copy=False, readonly=True, groups='base.group_system')
    release_prev_state = fields.Char(copy=False, readonly=True)
    release_pending_user_ids = fields.Many2many(
        'res.users', string='Waiting For', compute='_compute_release_pending_user_ids')
    release_can_approve = fields.Boolean(compute='_compute_release_permissions')
    release_can_reset = fields.Boolean(compute='_compute_release_permissions')

    # ------------------------------------------------------------------
    # Computes
    # ------------------------------------------------------------------
    @api.depends('company_id.po_release_strategy_enabled')
    def _compute_release_required(self):
        for order in self:
            order.release_required = order.company_id.po_release_strategy_enabled

    @api.depends('release_line_ids.state')
    def _compute_release_pending_user_ids(self):
        for order in self:
            users = self.env['res.users']
            for line in order.release_line_ids.filtered(lambda l: l.state == 'pending'):
                users |= line._get_approver_users()
            order.release_pending_user_ids = users

    @api.depends_context('uid')
    @api.depends('release_state', 'release_line_ids.state', 'user_id', 'state')
    def _compute_release_permissions(self):
        user = self.env.user
        is_manager = self._po_release_is_manager()
        for order in self:
            order.release_can_approve = bool(
                order.release_state == 'pending'
                and order._po_release_lines_for_user(user))
            order.release_can_reset = bool(
                order.release_state in ('pending', 'approved', 'rejected')
                and order.state in ('draft', 'sent', 'to approve')
                and (is_manager or order.user_id == user))

    @api.onchange('company_id')
    def _onchange_company_id_release_strategy(self):
        for order in self:
            if order.release_state == 'draft' and (
                    not order.release_strategy_id
                    or order.release_strategy_id.company_id != order.company_id):
                order.release_strategy_id = self._po_release_default_strategy(order.company_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @api.model
    def _po_release_default_strategy(self, company):
        if company and company.po_release_strategy_enabled:
            return company.po_release_default_strategy_id
        return self.env['po.release.strategy']

    def _po_release_is_required(self):
        self.ensure_one()
        return bool(self.company_id.po_release_strategy_enabled)

    def _po_release_is_manager(self):
        return (self.env.user.has_group('purchase.group_purchase_manager')
                or self.env.user.has_group('po_release_strategy.group_po_release_supervisor'))

    def _po_release_lines_for_user(self, user):
        self.ensure_one()
        return self.release_line_ids.filtered(
            lambda l: l.state == 'pending' and user in l._get_approver_users())

    def _po_release_get_strategy(self):
        self.ensure_one()
        strategy = self.release_strategy_id or self.company_id.po_release_default_strategy_id
        if not strategy:
            raise UserError(_(
                'A release strategy is mandatory for purchase order %s. Select one '
                'on the order or set a default strategy in the Purchase settings.',
                self.name))
        if strategy.company_id != self.company_id:
            raise UserError(_(
                'The release strategy "%(strategy)s" belongs to another company '
                'than purchase order %(order)s.', strategy=strategy.name, order=self.name))
        return strategy

    def _po_release_compute_amount(self, strategy):
        """Order amount normalized to the company currency."""
        self.ensure_one()
        amount = self.amount_untaxed if strategy.amount_basis == 'untaxed' else self.amount_total
        company_currency = self.company_id.currency_id
        if self.currency_id and self.currency_id != company_currency:
            amount = self.currency_id._convert(
                amount, company_currency, self.company_id,
                self.date_order or fields.Date.context_today(self))
        return company_currency.round(amount)

    @api.model
    def _po_release_lockable_field_names(self):
        names = []
        for name, field in self._fields.items():
            if name in NON_LOCKABLE_FIELDS or not field.store:
                continue
            if name.startswith(('message_', 'activity_', 'release_', 'website_')):
                continue
            if field.type == 'binary':
                continue
            if field.type == 'one2many' and name != 'order_line':
                continue
            if field.compute and name not in LOCKABLE_COMPUTED_FIELDS:
                continue
            if field.related:
                continue
            names.append(name)
        return names

    # ----- locked fields snapshot -----
    @staticmethod
    def _po_release_norm(value):
        if isinstance(value, float):
            return float_round(value, precision_digits=6)
        return value

    def _po_release_field_value(self, field_name):
        field = self._fields[field_name]
        value = self[field_name]
        norm = self._po_release_norm
        if field_name == 'order_line':
            return [
                [line.id, line.display_type or '', line.product_id.id, line.name or '',
                 norm(line.product_qty), line.product_uom.id, norm(line.price_unit),
                 sorted(line.taxes_id.ids)]
                for line in value.sorted('id')
            ]
        if field.type == 'many2one':
            return value.id
        if field.type in ('one2many', 'many2many'):
            return sorted(value.ids)
        if field.type == 'monetary':
            return norm(self.currency_id.round(value) if self.currency_id else value)
        if field.type == 'date':
            return fields.Date.to_string(value) if value else False
        if field.type == 'datetime':
            return fields.Datetime.to_string(value) if value else False
        if field.type in ('html', 'text', 'char'):
            return str(value) if value else False
        if field.type == 'properties':
            return json.dumps(value, sort_keys=True, default=str)
        return norm(value)

    def _po_release_build_snapshot(self):
        self.ensure_one()
        names = self.release_strategy_id.sudo().locked_field_ids.mapped('name')
        return {name: self._po_release_field_value(name) for name in names
                if name in self._fields}

    def _po_release_store_snapshot(self):
        for order in self:
            snapshot = order._po_release_build_snapshot()
            order.sudo().with_context(**{SKIP_CHECK_CTX: True}).write({
                'release_snapshot': json.dumps(snapshot, sort_keys=True, default=str)
                if snapshot else False,
            })

    def _po_release_changed_fields(self):
        """Names of locked fields whose value differs from the snapshot."""
        self.ensure_one()
        raw = self.sudo().release_snapshot
        if not raw:
            return []
        stored = json.loads(raw)
        current = json.loads(json.dumps(self._po_release_build_snapshot(),
                                        sort_keys=True, default=str))
        return [name for name in stored if stored.get(name) != current.get(name)]

    def _po_release_check_locked_changes(self):
        if self.env.context.get(SKIP_CHECK_CTX) or self.env.context.get(DEFER_CHECK_CTX):
            return
        orders = self.exists().filtered(
            lambda o: o.release_state in ('pending', 'approved')
            and o.state != 'cancel' and o.sudo().release_snapshot)
        for order in orders:
            changed = order._po_release_changed_fields()
            if not changed:
                continue
            labels = [order._fields[n].get_description(self.env)['string'] for n in changed]
            if (order.state in ('purchase', 'done')
                    and order.release_strategy_id.lock_confirmed_policy == 'block'):
                raise UserError(_(
                    'Purchase order %(order)s is released and confirmed. The '
                    'following fields are locked by its release strategy: %(fields)s',
                    order=order.name, fields=', '.join(labels)))
            order._po_release_reset(changed_labels=labels, resubmit=True)

    # ------------------------------------------------------------------
    # ORM
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('release_strategy_id'):
                continue
            company = self.env['res.company'].browse(
                vals.get('company_id') or self.env.company.id)
            strategy = self._po_release_default_strategy(company)
            if strategy:
                vals['release_strategy_id'] = strategy.id
        return super().create(vals_list)

    def write(self, vals):
        if 'release_strategy_id' in vals and not self.env.context.get(SKIP_CHECK_CTX):
            for order in self:
                if (order.release_state in ('pending', 'approved')
                        and order.release_strategy_id.id != vals['release_strategy_id']):
                    raise UserError(_(
                        'Reset the release strategy of %s before changing it.', order.name))
        if self.env.context.get(SKIP_CHECK_CTX):
            return super().write(vals)
        res = super(PurchaseOrder, self.with_context(**{DEFER_CHECK_CTX: True})).write(vals)
        self._po_release_check_locked_changes()
        return res

    @api.constrains('release_strategy_id', 'company_id')
    def _check_release_strategy_company(self):
        for order in self:
            strategy = order.release_strategy_id
            if strategy and strategy.company_id != order.company_id:
                raise ValidationError(_(
                    'The release strategy of %s must belong to the same company.',
                    order.name))

    # ------------------------------------------------------------------
    # Native workflow integration
    # ------------------------------------------------------------------
    def _approval_allowed(self):
        self.ensure_one()
        if self._po_release_is_required():
            return self.release_state == 'approved'
        return super()._approval_allowed()

    def button_confirm(self):
        to_release = self.filtered(
            lambda o: o.state in ('draft', 'sent') and o._po_release_is_required()
            and o.release_state != 'approved')
        if to_release:
            for order in to_release:
                order.order_line._validate_analytic_distribution()
            to_release._po_release_submit()
        res = super(PurchaseOrder, self - to_release).button_confirm()
        (self - to_release).filtered(lambda o: o.state in ('purchase', 'done'))._po_release_close_buyer_activity()
        return res

    def button_approve(self, force=False):
        if not force:
            blocked = self.filtered(
                lambda o: o._po_release_is_required() and o.release_state != 'approved')
            if blocked:
                raise UserError(_(
                    'Purchase order(s) %s must be released through their release '
                    'strategy before confirmation.', ', '.join(blocked.mapped('name'))))
        return super().button_approve(force=force)

    def button_cancel(self):
        res = super().button_cancel()
        self.filtered(lambda o: o.state == 'cancel')._po_release_clear()
        return res

    # ------------------------------------------------------------------
    # Release engine
    # ------------------------------------------------------------------
    def _po_release_prepare_lines(self, strategy, amount):
        self.ensure_one()
        return [{
            'order_id': self.id,
            'rule_id': rule.id,
            'sequence': rule.sequence,
            'role': rule.role,
            'approver_type': rule.approver_type,
            'user_id': rule.user_id.id,
            'group_id': rule.group_id.id,
            'approver_display': rule._get_approver_display(),
            'min_amount': rule.min_amount,
            'max_amount': rule.max_amount,
            'state': 'waiting',
        } for rule in strategy._get_applicable_rules(amount)]

    def _po_release_clear(self):
        """Drop approval lines/activities and go back to 'Not Submitted'."""
        for order in self.sudo().with_context(**{SKIP_CHECK_CTX: True, RELEASE_CTX: True}):
            order.release_line_ids.unlink()
            order.write({
                'release_state': 'draft',
                'release_amount_company': 0.0,
                'release_submit_date': False,
                'release_approval_date': False,
                'release_bypassed': False,
                'release_bypass_user_id': False,
                'release_bypass_reason': False,
                'release_snapshot': False,
            })
            order._po_release_close_buyer_activity()

    def _po_release_submit(self):
        """Generate the approval lines and start the first stage."""
        for order in self:
            if order.state == 'cancel':
                raise UserError(_('Cancelled order %s cannot be submitted.', order.name))
            strategy = order._po_release_get_strategy()
            sudo_order = order.sudo().with_context(**{SKIP_CHECK_CTX: True, RELEASE_CTX: True})
            amount = order._po_release_compute_amount(strategy)
            sudo_order.release_line_ids.unlink()
            vals = {
                'release_strategy_id': strategy.id,
                'release_state': 'pending',
                'release_amount_company': amount,
                'release_submit_date': fields.Datetime.now(),
                'release_approval_date': False,
                'release_bypassed': False,
                'release_bypass_user_id': False,
                'release_bypass_reason': False,
            }
            if order.state in ('draft', 'sent'):
                vals['release_prev_state'] = order.state
                vals['state'] = 'to approve'
            sudo_order.write(vals)
            if order.partner_id not in order.message_partner_ids:
                sudo_order.message_subscribe([order.partner_id.id])
            sudo_order.env['purchase.order.release.line'].create(
                order._po_release_prepare_lines(strategy, amount))
            order._po_release_store_snapshot()
            if not sudo_order.release_line_ids:
                sudo_order.message_post(body=_(
                    'No approval rule of release strategy "%s" applies to this '
                    'amount: the order is released automatically.', strategy.name))
            else:
                sudo_order.message_post(body=_(
                    'Order submitted for release approval (%(strategy)s).',
                    strategy=strategy.name))
            sudo_order._po_release_activate_next_stage()
        return True

    def _po_release_activate_next_stage(self):
        for order in self:
            lines = order.release_line_ids
            if lines.filtered(lambda l: l.state == 'pending'):
                continue
            waiting = lines.filtered(lambda l: l.state == 'waiting')
            if not waiting:
                order._po_release_finalize()
                continue
            stage = min(waiting.mapped('sequence'))
            waiting.filtered(lambda l: l.sequence == stage)._activate()

    def _po_release_finalize(self):
        """All stages approved (or bypassed): apply the post-approval action."""
        for order in self.sudo().with_context(**{SKIP_CHECK_CTX: True, RELEASE_CTX: True}):
            order.write({
                'release_state': 'approved',
                'release_approval_date': fields.Datetime.now(),
            })
            strategy = order.release_strategy_id
            if order.state in ('purchase', 'done'):
                order.message_post(body=_('Release strategy approved again.'))
                order._po_release_notify_buyer(_('The order was re-approved.'))
                continue
            if strategy.post_approval_action == 'auto_confirm':
                if order.state in ('draft', 'sent'):
                    order.write({'state': 'to approve'})
                order._add_supplier_to_product()
                order.button_approve()
                order.message_post(body=_('Order released and confirmed automatically.'))
            else:
                prev_state = order.release_prev_state or 'draft'
                if order.state == 'to approve':
                    order.write({'state': prev_state if prev_state in ('draft', 'sent') else 'draft'})
                order.message_post(body=_('Order released. Waiting for the buyer to confirm it.'))
                order._po_release_notify_buyer(
                    _('Released: please confirm this purchase order.'), confirm=True)

    def _po_release_notify_buyer(self, summary, confirm=False):
        for order in self:
            buyer = order.user_id
            if not buyer or not buyer.active or buyer.share:
                continue
            xmlid = ('po_release_strategy.mail_activity_type_po_release_confirm'
                     if confirm else 'mail.mail_activity_data_todo')
            order.sudo().activity_schedule(xmlid, summary=summary, user_id=buyer.id)

    def _po_release_close_buyer_activity(self):
        self.sudo().activity_unlink(['po_release_strategy.mail_activity_type_po_release_confirm'])

    def _po_release_reset(self, reason=False, changed_labels=None, resubmit=False):
        """Clear every approval; optionally resubmit and re-notify approvers."""
        for order in self:
            sudo_order = order.sudo().with_context(**{SKIP_CHECK_CTX: True, RELEASE_CTX: True})
            previous_users = self.env['res.users']
            for line in order.release_line_ids:
                previous_users |= line._get_approver_users()
            was_confirmed = order.state in ('purchase', 'done')
            prev_state = order.release_prev_state
            sudo_order._po_release_clear()
            sudo_order.release_reset_count += 1
            if not was_confirmed and order.state == 'to approve':
                sudo_order.write({'state': prev_state if prev_state in ('draft', 'sent') else 'draft'})
            if changed_labels:
                body = Markup('<p>%s</p><ul>%s</ul>') % (
                    _('Release strategy reset: the following locked fields were modified. '
                      'Previous approvals have been cleared.'),
                    Markup('').join(Markup('<li>%s</li>') % escape(l) for l in changed_labels))
            else:
                body = Markup('<p>%s</p>') % _('Release strategy reset by %s.', self.env.user.name)
                if reason:
                    body += Markup('<p>%s</p>') % reason
            if resubmit:
                # a confirmed order keeps its state and only requests a new approval
                sudo_order._po_release_submit()
            for line in sudo_order.release_line_ids:
                previous_users |= line._get_approver_users()
            sudo_order.message_post(
                body=body,
                partner_ids=previous_users.partner_id.ids,
                subtype_xmlid='mail.mt_note')

    # ------------------------------------------------------------------
    # User actions (called by buttons / wizard)
    # ------------------------------------------------------------------
    def _po_release_open_wizard(self, action):
        self.ensure_one()
        titles = {
            'approve': _('Approve Release'),
            'reject': _('Reject Release'),
            'bypass': _('Bypass Release Approval'),
            'reset': _('Reset Release Strategy'),
        }
        return {
            'type': 'ir.actions.act_window',
            'name': titles[action],
            'res_model': 'po.release.action.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_order_id': self.id, 'default_action': action},
        }

    def action_release_submit(self):
        for order in self:
            if not order._po_release_is_required():
                raise UserError(_('Release strategies are not enabled for %s.',
                                  order.company_id.name))
            if order.state not in ('draft', 'sent'):
                raise UserError(_('Only requests for quotation can be submitted.'))
            if order.release_state not in ('draft', 'rejected'):
                raise UserError(_('%s is already submitted.', order.name))
            order.order_line._validate_analytic_distribution()
        self._po_release_submit()
        return True

    def action_release_approve(self):
        return self._po_release_open_wizard('approve')

    def action_release_reject(self):
        return self._po_release_open_wizard('reject')

    def action_release_bypass(self):
        return self._po_release_open_wizard('bypass')

    def action_release_reset(self):
        return self._po_release_open_wizard('reset')

    def _po_release_approve(self, notes=False):
        user = self.env.user
        for order in self:
            if order.release_state != 'pending':
                raise UserError(_('%s is not waiting for release approval.', order.name))
            lines = order._po_release_lines_for_user(user)
            if not lines:
                raise AccessError(_(
                    'You are not an approver of the current release stage of %s.',
                    order.name))
            sudo_lines = lines.sudo().with_context(**{RELEASE_CTX: True, SKIP_CHECK_CTX: True})
            sudo_lines._mark_approved(user, notes)
            sudo_order = order.sudo().with_context(**{RELEASE_CTX: True, SKIP_CHECK_CTX: True})
            sudo_order._po_release_activate_next_stage()
        return True

    def _po_release_reject(self, notes):
        if not notes:
            raise UserError(_('Please give a reason for the rejection.'))
        user = self.env.user
        for order in self:
            if order.release_state != 'pending':
                raise UserError(_('%s is not waiting for release approval.', order.name))
            lines = order._po_release_lines_for_user(user)
            if not lines:
                raise AccessError(_(
                    'You are not an approver of the current release stage of %s.',
                    order.name))
            sudo_order = order.sudo().with_context(**{RELEASE_CTX: True, SKIP_CHECK_CTX: True})
            lines.sudo().with_context(**{RELEASE_CTX: True})._mark_rejected(user, notes)
            (sudo_order.release_line_ids - lines)._cancel()
            vals = {'release_state': 'rejected', 'release_snapshot': False}
            if order.state == 'to approve':
                prev_state = order.release_prev_state
                vals['state'] = prev_state if prev_state in ('draft', 'sent') else 'draft'
            sudo_order.write(vals)
            sudo_order.message_post(body=Markup('<p>%s</p><p>%s</p>') % (
                _('Release rejected by %s.', user.name), notes))
            sudo_order._po_release_notify_buyer(_('Release rejected: %s', notes))
        return True

    def _po_release_bypass(self, notes):
        if not self.env.user.has_group('po_release_strategy.group_po_release_supervisor'):
            raise AccessError(_('Only release supervisors can bypass a release strategy.'))
        if not notes:
            raise UserError(_('Please give a reason for the bypass.'))
        user = self.env.user
        for order in self:
            if order.release_state not in ('pending', 'rejected', 'draft'):
                raise UserError(_('%s is already released.', order.name))
            if order.release_state != 'pending':
                order._po_release_submit()
                if order.release_state == 'approved':
                    continue
            sudo_order = order.sudo().with_context(**{RELEASE_CTX: True, SKIP_CHECK_CTX: True})
            sudo_order.release_line_ids.filtered(
                lambda l: l.state in ('waiting', 'pending'))._mark_approved(user, notes, bypass=True)
            sudo_order.write({
                'release_bypassed': True,
                'release_bypass_user_id': user.id,
                'release_bypass_reason': notes,
            })
            sudo_order.message_post(body=Markup('<p>%s</p><p>%s</p>') % (
                _('Release approval bypassed by supervisor %s.', user.name), notes))
            sudo_order._po_release_finalize()
        return True

    def _po_release_manual_reset(self, notes=False):
        for order in self:
            if not order.release_can_reset:
                raise AccessError(_(
                    'Only the buyer, purchase managers or release supervisors can '
                    'reset the release strategy of %s.', order.name))
            order._po_release_reset(reason=notes, resubmit=False)
        return True


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    def _po_release_check_orders(self, orders):
        ctx = self.env.context
        if ctx.get(SKIP_CHECK_CTX) or ctx.get(DEFER_CHECK_CTX):
            return
        orders._po_release_check_locked_changes()

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        self._po_release_check_orders(lines.order_id)
        return lines

    def write(self, vals):
        res = super().write(vals)
        self._po_release_check_orders(self.order_id)
        return res

    def unlink(self):
        orders = self.order_id
        res = super().unlink()
        self._po_release_check_orders(orders)
        return res
