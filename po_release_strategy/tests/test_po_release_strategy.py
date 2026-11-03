# -*- coding: utf-8 -*-
from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form, tagged
from odoo.tests.common import TransactionCase, new_test_user


@tagged('post_install', '-at_install')
class TestPoReleaseStrategy(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=False,
                                       mail_create_nolog=True, mail_notrack=False))
        cls.company = cls.env.company
        cls.company_currency = cls.company.currency_id

        purchase_user = 'purchase.group_purchase_user'
        cls.buyer = new_test_user(cls.env, 'rs_buyer', groups=f'base.group_user,{purchase_user}',
                                  name='Release Buyer')
        cls.manager = new_test_user(cls.env, 'rs_manager', groups=f'base.group_user,{purchase_user}',
                                    name='Release Manager')
        cls.fin1 = new_test_user(cls.env, 'rs_fin1', groups=f'base.group_user,{purchase_user}',
                                 name='Finance One')
        cls.fin2 = new_test_user(cls.env, 'rs_fin2', groups=f'base.group_user,{purchase_user}',
                                 name='Finance Two')
        cls.outsider = new_test_user(cls.env, 'rs_outsider', groups=f'base.group_user,{purchase_user}',
                                     name='Outsider')
        cls.supervisor = new_test_user(
            cls.env, 'rs_supervisor', name='Release Supervisor',
            groups=f'base.group_user,{purchase_user},po_release_strategy.group_po_release_supervisor')
        cls.finance_group = cls.env['res.groups'].create({
            'name': 'Release Finance Approvers',
            'users': [Command.link(cls.fin1.id), Command.link(cls.fin2.id)],
        })

        cls.partner = cls.env['res.partner'].create({'name': 'Release Vendor'})
        cls.partner2 = cls.env['res.partner'].create({'name': 'Other Vendor'})
        cls.product = cls.env['product.product'].create({
            'name': 'Release Product', 'type': 'consu', 'standard_price': 10.0,
            'taxes_id': False, 'supplier_taxes_id': False,
        })

        fields_model = cls.env['ir.model.fields']
        cls.f_partner_ref = fields_model._get('purchase.order', 'partner_ref')
        cls.f_order_line = fields_model._get('purchase.order', 'order_line')
        cls.f_amount_total = fields_model._get('purchase.order', 'amount_total')

        cls.strategy = cls.env['po.release.strategy'].create({
            'name': 'Standard',
            'post_approval_action': 'auto_confirm',
            'locked_field_ids': [Command.set([cls.f_partner_ref.id, cls.f_order_line.id])],
            'rule_ids': [
                Command.create({'sequence': 10, 'role': 'Purchase Manager',
                                'approver_type': 'user', 'user_id': cls.manager.id,
                                'min_amount': 0.0, 'max_amount': 0.0}),
                Command.create({'sequence': 20, 'role': 'Finance',
                                'approver_type': 'group', 'group_id': cls.finance_group.id,
                                'min_amount': 10000.0, 'max_amount': 0.0}),
            ],
        })
        cls.company.write({
            'po_release_strategy_enabled': True,
            'po_release_default_strategy_id': cls.strategy.id,
        })
        cls.activity_type = cls.env.ref('po_release_strategy.mail_activity_type_po_release')
        cls.confirm_type = cls.env.ref('po_release_strategy.mail_activity_type_po_release_confirm')

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _create_po(self, price=100.0, qty=1.0, currency=None, user=None, **vals):
        values = {
            'partner_id': self.partner.id,
            'user_id': self.buyer.id,
            'order_line': [Command.create({
                'product_id': self.product.id,
                'product_qty': qty,
                'price_unit': price,
                'taxes_id': False,
            })],
        }
        if currency:
            values['currency_id'] = currency.id
        values.update(vals)
        return self.env['purchase.order'].with_user(user or self.buyer).create(values)

    def _release_activities(self, order, user=None):
        activities = order.sudo().activity_ids.filtered(
            lambda a: a.activity_type_id == self.activity_type)
        if user:
            activities = activities.filtered(lambda a: a.user_id == user)
        return activities

    def _wizard(self, order, user, action, notes=False):
        res = order.with_user(user)._po_release_open_wizard(action)
        wizard = self.env['po.release.action.wizard'].with_user(user).with_context(
            **res['context']).create({'notes': notes})
        return wizard.action_confirm()

    # ------------------------------------------------------------------
    # configuration
    # ------------------------------------------------------------------
    def test_00_disabled_keeps_native_flow(self):
        self.company.po_release_strategy_enabled = False
        po = self._create_po(price=50000)
        self.assertFalse(po.release_required)
        po.with_user(self.manager).button_confirm()
        self.assertIn(po.state, ('purchase', 'done', 'to approve'))
        self.assertEqual(po.release_state, 'draft')
        self.assertFalse(po.release_line_ids)

    def test_01_default_strategy_applied(self):
        po = self._create_po()
        self.assertTrue(po.release_required)
        self.assertEqual(po.release_strategy_id, self.strategy)
        self.assertTrue(self.strategy.is_default)

    def test_02_strategy_mandatory(self):
        self.company.po_release_default_strategy_id = False
        po = self._create_po()
        self.assertFalse(po.release_strategy_id)
        with self.assertRaises(UserError):
            po.button_confirm()

    def test_03_rule_constraints(self):
        with self.assertRaises(ValidationError):
            self.env['po.release.strategy.rule'].create({
                'strategy_id': self.strategy.id, 'role': 'Bad', 'approver_type': 'user',
                'user_id': self.manager.id, 'min_amount': 500.0, 'max_amount': 100.0})
        with self.assertRaises(ValidationError):
            self.env['po.release.strategy.rule'].create({
                'strategy_id': self.strategy.id, 'role': 'No group', 'approver_type': 'group'})
        state_field = self.env['ir.model.fields']._get('purchase.order', 'state')
        with self.assertRaises(ValidationError):
            self.strategy.locked_field_ids = [Command.link(state_field.id)]
        self.assertIn(self.f_amount_total, self.strategy.lockable_field_ids)
        self.assertNotIn(state_field, self.strategy.lockable_field_ids)

    def test_04_settings_form(self):
        settings = Form(self.env['res.config.settings'])
        self.assertTrue(settings.po_release_strategy_enabled)
        self.assertEqual(settings.po_release_default_strategy_id, self.strategy)

    # ------------------------------------------------------------------
    # single stage / auto confirm
    # ------------------------------------------------------------------
    def test_10_small_order_single_stage(self):
        po = self._create_po(price=5000)
        po.button_confirm()
        self.assertEqual(po.state, 'to approve')
        self.assertEqual(po.release_state, 'pending')
        self.assertEqual(po.release_amount_company, 5000.0)
        self.assertEqual(len(po.release_line_ids), 1, 'finance rule must not apply below 10000')
        line = po.release_line_ids
        self.assertEqual(line.state, 'pending')
        self.assertTrue(line.assigned_date)
        activities = self._release_activities(po)
        self.assertEqual(activities.user_id, self.manager)
        self.assertEqual(activities.po_release_line_id, line)

        # nobody else can approve
        self.assertFalse(po.with_user(self.outsider).release_can_approve)
        with self.assertRaises(AccessError):
            po.with_user(self.outsider)._po_release_approve()
        # native approve button is blocked
        with self.assertRaises(UserError):
            po.with_user(self.supervisor).button_approve()

        self.assertTrue(po.with_user(self.manager).release_can_approve)
        self._wizard(po, self.manager, 'approve', 'Budget OK')
        self.assertEqual(line.state, 'approved')
        self.assertEqual(line.approved_by_id, self.manager)
        self.assertEqual(line.notes, 'Budget OK')
        self.assertTrue(line.approval_date)
        self.assertFalse(self._release_activities(po), 'activity must be completed')
        done_msg = po.message_ids.filtered(
            lambda m: m.mail_activity_type_id == self.activity_type)
        self.assertTrue(done_msg, 'completing the activity posts a "done" message')
        self.assertEqual(po.release_state, 'approved')
        self.assertIn(po.state, ('purchase', 'done'))
        self.assertTrue(po.release_approval_date)

    def test_11_no_rule_applies_auto_release(self):
        self.strategy.rule_ids.filtered(lambda r: r.sequence == 10).min_amount = 1000.0
        po = self._create_po(price=10)
        po.button_confirm()
        self.assertEqual(po.release_state, 'approved')
        self.assertIn(po.state, ('purchase', 'done'))
        self.assertFalse(po.release_line_ids)

    # ------------------------------------------------------------------
    # multi stage / groups
    # ------------------------------------------------------------------
    def test_20_multi_stage_with_group(self):
        po = self._create_po(price=20000)
        po.with_user(self.buyer).action_release_submit()
        self.assertEqual(len(po.release_line_ids), 2)
        manager_line, finance_line = po.release_line_ids.sorted('sequence')
        self.assertEqual(manager_line.state, 'pending')
        self.assertEqual(finance_line.state, 'waiting')
        self.assertEqual(self._release_activities(po).user_id, self.manager)
        self.assertEqual(po.release_pending_user_ids, self.manager)

        # finance cannot approve before its stage
        with self.assertRaises(AccessError):
            po.with_user(self.fin1)._po_release_approve()

        self._wizard(po, self.manager, 'approve')
        self.assertEqual(finance_line.state, 'pending')
        self.assertEqual(self._release_activities(po).user_id, self.fin1 | self.fin2)
        self.assertEqual(po.state, 'to approve')

        self._wizard(po, self.fin2, 'approve', 'Finance validated')
        self.assertEqual(finance_line.state, 'approved')
        self.assertEqual(finance_line.approved_by_id, self.fin2)
        self.assertFalse(self._release_activities(po), "other members' activities removed")
        self.assertIn(po.state, ('purchase', 'done'))

    def test_21_parallel_stage(self):
        self.strategy.rule_ids.filtered(lambda r: r.role == 'Finance').write(
            {'sequence': 10, 'min_amount': 0.0})
        po = self._create_po(price=100)
        po.button_confirm()
        self.assertEqual(set(po.release_line_ids.mapped('state')), {'pending'})
        self._wizard(po, self.fin1, 'approve')
        self.assertEqual(po.release_state, 'pending')
        self._wizard(po, self.manager, 'approve')
        self.assertEqual(po.release_state, 'approved')

    def test_22_group_member_without_access_skipped(self):
        portal_less = new_test_user(self.env, 'rs_noaccess', groups='base.group_user', name='No Access')
        self.finance_group.users = [Command.link(portal_less.id)]
        po = self._create_po(price=20000)
        po.button_confirm()
        self._wizard(po, self.manager, 'approve')
        self.assertNotIn(portal_less, self._release_activities(po).user_id)

    # ------------------------------------------------------------------
    # currency normalization
    # ------------------------------------------------------------------
    def test_30_foreign_currency_normalized(self):
        foreign = self.env['res.currency'].create({
            'name': 'RSX', 'symbol': 'R', 'rounding': 0.01})
        rates = self.env['res.currency.rate']
        own_rate = rates.search([('currency_id', '=', self.company_currency.id),
                                 ('company_id', '=', self.company.id),
                                 ('name', '=', fields.Date.today())])
        if own_rate:
            own_rate.rate = 1.0
        else:
            rates.create({'currency_id': self.company_currency.id, 'company_id': self.company.id,
                          'name': fields.Date.today(), 'rate': 1.0})
        rates.create({'currency_id': foreign.id, 'company_id': self.company.id,
                      'name': fields.Date.today(), 'rate': 0.5})  # 1 company unit = 0.5 RSX
        # 6000 RSX = 12000 in company currency -> finance stage needed
        po = self._create_po(price=6000, currency=foreign)
        po.button_confirm()
        self.assertEqual(po.release_amount_company, 12000.0)
        self.assertEqual(len(po.release_line_ids), 2)
        # same face value in company currency stays below the threshold
        po2 = self._create_po(price=6000)
        po2.button_confirm()
        self.assertEqual(len(po2.release_line_ids), 1)

    def test_31_untaxed_basis(self):
        tax = self.env['account.tax'].create({
            'name': 'RS 100%', 'amount': 100.0, 'type_tax_use': 'purchase'})
        po = self._create_po(price=6000)
        po.order_line.taxes_id = tax
        self.assertEqual(po.amount_total, 12000.0)
        po.button_confirm()
        self.assertEqual(len(po.release_line_ids), 2)
        po.with_user(self.supervisor)._po_release_manual_reset()
        self.strategy.amount_basis = 'untaxed'
        po.button_confirm()
        self.assertEqual(po.release_amount_company, 6000.0)
        self.assertEqual(len(po.release_line_ids), 1)

    # ------------------------------------------------------------------
    # reject / reset / bypass
    # ------------------------------------------------------------------
    def test_40_reject_and_resubmit(self):
        po = self._create_po(price=20000)
        po.button_confirm()
        with self.assertRaises(UserError):
            self._wizard(po, self.manager, 'reject', '')
        self._wizard(po, self.manager, 'reject', 'Price too high')
        manager_line, finance_line = po.release_line_ids.sorted('sequence')
        self.assertEqual(manager_line.state, 'rejected')
        self.assertEqual(manager_line.rejected_by_id, self.manager)
        self.assertEqual(finance_line.state, 'cancelled')
        self.assertEqual(po.release_state, 'rejected')
        self.assertEqual(po.state, 'draft')
        self.assertFalse(self._release_activities(po))
        todo = po.activity_ids.filtered(lambda a: a.user_id == self.buyer)
        self.assertTrue(todo, 'buyer is notified of the rejection')

        po.order_line.price_unit = 900
        po.with_user(self.buyer).action_release_submit()
        self.assertEqual(po.release_state, 'pending')
        self.assertEqual(len(po.release_line_ids), 1)

    def test_41_manual_reset(self):
        po = self._create_po(price=20000)
        po.button_confirm()
        self._wizard(po, self.manager, 'approve')
        with self.assertRaises(AccessError):
            po.with_user(self.outsider)._po_release_manual_reset()
        self._wizard(po, self.buyer, 'reset', 'Wrong vendor')
        self.assertEqual(po.release_state, 'draft')
        self.assertEqual(po.state, 'draft')
        self.assertFalse(po.release_line_ids)
        self.assertFalse(self._release_activities(po))
        self.assertEqual(po.release_reset_count, 1)

    def test_42_supervisor_bypass(self):
        po = self._create_po(price=20000)
        po.button_confirm()
        with self.assertRaises(AccessError):
            po.with_user(self.manager)._po_release_bypass('urgent')
        with self.assertRaises(UserError):
            self._wizard(po, self.supervisor, 'bypass', '')
        self._wizard(po, self.supervisor, 'bypass', 'Emergency purchase')
        self.assertEqual(set(po.release_line_ids.mapped('state')), {'bypassed'})
        self.assertTrue(po.release_bypassed)
        self.assertEqual(po.release_bypass_user_id, self.supervisor)
        self.assertEqual(po.release_bypass_reason, 'Emergency purchase')
        self.assertFalse(self._release_activities(po))
        self.assertIn(po.state, ('purchase', 'done'))

    def test_43_activity_cannot_be_completed_manually(self):
        po = self._create_po(price=100)
        po.button_confirm()
        activity = self._release_activities(po).with_user(self.manager)
        with self.assertRaises(UserError):
            activity.action_feedback(feedback='done from chatter')
        with self.assertRaises(UserError):
            activity.unlink()

    def test_44_cancel_clears_release(self):
        po = self._create_po(price=20000)
        po.button_confirm()
        po.button_cancel()
        self.assertEqual(po.state, 'cancel')
        self.assertEqual(po.release_state, 'draft')
        self.assertFalse(po.release_line_ids)
        self.assertFalse(self._release_activities(po))
        po.button_draft()
        po.button_confirm()
        self.assertEqual(po.release_state, 'pending')

    def test_45_copy_resets_release(self):
        po = self._create_po(price=100)
        po.button_confirm()
        self._wizard(po, self.manager, 'approve')
        copy = po.copy()
        self.assertEqual(copy.release_state, 'draft')
        self.assertFalse(copy.release_line_ids)
        self.assertEqual(copy.release_strategy_id, self.strategy)

    # ------------------------------------------------------------------
    # post-approval: notify buyer
    # ------------------------------------------------------------------
    def test_50_notify_buyer(self):
        self.strategy.post_approval_action = 'notify_buyer'
        po = self._create_po(price=100)
        po.button_confirm()
        self._wizard(po, self.manager, 'approve')
        self.assertEqual(po.release_state, 'approved')
        self.assertEqual(po.state, 'draft')
        confirm_activity = po.activity_ids.filtered(lambda a: a.activity_type_id == self.confirm_type)
        self.assertEqual(confirm_activity.user_id, self.buyer)
        po.with_user(self.buyer).button_confirm()
        self.assertIn(po.state, ('purchase', 'done'))
        self.assertFalse(po.activity_ids.filtered(lambda a: a.activity_type_id == self.confirm_type))

    # ------------------------------------------------------------------
    # field locking
    # ------------------------------------------------------------------
    def test_60_locked_field_resets_and_renotifies(self):
        self.strategy.post_approval_action = 'notify_buyer'
        po = self._create_po(price=20000)
        po.button_confirm()
        self._wizard(po, self.manager, 'approve')
        self._wizard(po, self.fin1, 'approve')
        self.assertEqual(po.release_state, 'approved')
        self.assertEqual(po.state, 'draft')
        messages_before = po.message_ids

        po.with_user(self.buyer).write({'partner_ref': 'NEW-REF'})
        self.assertEqual(po.release_state, 'pending')
        self.assertEqual(po.state, 'to approve')
        self.assertEqual(po.release_reset_count, 1)
        self.assertEqual(set(po.release_line_ids.mapped('state')), {'pending', 'waiting'})
        self.assertFalse(po.release_line_ids.filtered('approved_by_id'), 'approvals cleared')
        self.assertEqual(self._release_activities(po).user_id, self.manager)
        reset_msg = (po.message_ids - messages_before).filtered(
            lambda m: 'locked fields were modified' in (m.body or ''))
        self.assertTrue(reset_msg)
        self.assertEqual(set(reset_msg.partner_ids.ids),
                         set((self.manager | self.fin1 | self.fin2).partner_id.ids))

    def test_61_locked_order_lines(self):
        po = self._create_po(price=100)
        po.button_confirm()  # pending, snapshot taken at submission
        po.order_line.with_user(self.buyer).write({'product_qty': 5})
        self.assertEqual(po.release_reset_count, 1)
        self.assertEqual(po.release_state, 'pending')
        # adding a line also resets
        po.with_user(self.buyer).write({'order_line': [Command.create({
            'product_id': self.product.id, 'product_qty': 1, 'price_unit': 1, 'taxes_id': False})]})
        self.assertEqual(po.release_reset_count, 2)

    def test_62_unlocked_field_does_not_reset(self):
        po = self._create_po(price=100)
        po.button_confirm()
        po.write({'notes': '<p>internal remark</p>', 'date_planned': fields.Datetime.now()})
        self.assertEqual(po.release_reset_count, 0)
        self.assertEqual(po.release_state, 'pending')

    def test_63_confirmed_order_reset_policy(self):
        po = self._create_po(price=100)
        po.button_confirm()
        self._wizard(po, self.manager, 'approve')
        self.assertIn(po.state, ('purchase', 'done'))
        po.button_unlock() if po.state == 'done' else None
        po.order_line.write({'price_unit': 150})
        self.assertEqual(po.state, 'purchase', 'confirmed order keeps its state')
        self.assertEqual(po.release_state, 'pending')
        self.assertEqual(self._release_activities(po).user_id, self.manager)
        self._wizard(po, self.manager, 'approve')
        self.assertEqual(po.release_state, 'approved')
        self.assertEqual(po.state, 'purchase')

    def test_64_confirmed_order_block_policy(self):
        self.strategy.lock_confirmed_policy = 'block'
        po = self._create_po(price=100)
        po.button_confirm()
        self._wizard(po, self.manager, 'approve')
        po.button_unlock() if po.state == 'done' else None
        with self.assertRaises(UserError):
            po.write({'partner_ref': 'CHANGED'})
        po.write({'notes': '<p>allowed</p>'})
        self.assertEqual(po.release_state, 'approved')

    def test_65_strategy_change_needs_reset(self):
        other = self.strategy.copy()
        po = self._create_po(price=100)
        po.button_confirm()
        with self.assertRaises(UserError):
            po.release_strategy_id = other

    def test_66_lockable_fields_include_editable_computed(self):
        names = self.env['purchase.order']._po_release_lockable_field_names()
        # editable even when computed (computed + readonly=False since v18)
        for name in ('partner_id', 'currency_id', 'order_line', 'amount_total', 'payment_term_id'):
            self.assertIn(name, names)
        for name in ('state', 'name', 'invoice_status', 'mail_reception_declined', 'release_state'):
            self.assertNotIn(name, names)
        # locking the currency resets a pending release
        self.strategy.locked_field_ids = [Command.link(
            self.env['ir.model.fields']._get('purchase.order', 'currency_id').id)]
        po = self._create_po(price=100)
        po.button_confirm()
        other = self.env['res.currency'].create({'name': 'RSY', 'symbol': 'Y', 'rounding': 0.01})
        po.currency_id = other
        self.assertEqual(po.release_reset_count, 1)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def test_70_forms(self):
        with Form(self.env['purchase.order'].with_user(self.buyer)) as po_form:
            po_form.partner_id = self.partner
            self.assertEqual(po_form.release_strategy_id, self.strategy)
            with po_form.order_line.new() as line:
                line.product_id = self.product
                line.product_qty = 2
                line.price_unit = 10
        po = po_form.save()
        po.button_confirm()
        Form(po.with_user(self.manager))
        strategy_form = Form(self.strategy)
        with strategy_form.rule_ids.new() as rule:
            rule.role = 'CFO'
            rule.approver_type = 'user'
            rule.user_id = self.supervisor
            rule.min_amount = 100000
        strategy_form.save()
        self.assertEqual(len(self.strategy.rule_ids), 3)

    def test_71_my_approvals_filter(self):
        po = self._create_po(price=100)
        po.button_confirm()
        domain = [('activity_ids.user_id', '=', self.manager.id),
                  ('activity_ids.po_release_line_id', '!=', False)]
        found = self.env['purchase.order'].with_user(self.manager).search(domain)
        self.assertIn(po, found)

    # ------------------------------------------------------------------
    # integration with purchase_stock (skipped when not installed)
    # ------------------------------------------------------------------
    def test_80_receipt_created_only_after_release(self):
        if 'picking_ids' not in self.env['purchase.order']._fields:
            self.skipTest('purchase_stock is not installed')
        stockable = self.env['product.product'].create({
            'name': 'Release Stockable', 'type': 'consu', 'is_storable': True, 'taxes_id': False,
            'supplier_taxes_id': False})
        po = self._create_po(price=20000)
        po.order_line.product_id = stockable
        po.button_confirm()
        self.assertFalse(po.picking_ids, 'no receipt while waiting for approval')
        self._wizard(po, self.manager, 'approve')
        self.assertFalse(po.picking_ids)
        self._wizard(po, self.fin2, 'approve')
        self.assertEqual(len(po.picking_ids), 1)
        # receiving goods must not trigger the locked-field reset
        picking = po.picking_ids
        picking.move_ids.write({'quantity': 1, 'picked': True})
        picking.button_validate()
        self.assertEqual(po.order_line.qty_received, 1)
        self.assertEqual(po.release_state, 'approved')
        self.assertEqual(po.release_reset_count, 0)
