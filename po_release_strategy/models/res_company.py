# -*- coding: utf-8 -*-
from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    po_release_strategy_enabled = fields.Boolean(
        string='Purchase Release Strategies',
        default=False,
        help='When enabled, every purchase order of this company must go '
             'through a release strategy before it can be confirmed.')
    po_release_default_strategy_id = fields.Many2one(
        'po.release.strategy',
        string='Default Release Strategy',
        ondelete='set null',
        help='Release strategy applied automatically to new purchase orders.')
