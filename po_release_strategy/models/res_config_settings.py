# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    po_release_strategy_enabled = fields.Boolean(
        related='company_id.po_release_strategy_enabled', readonly=False)
    po_release_default_strategy_id = fields.Many2one(
        related='company_id.po_release_default_strategy_id', readonly=False,
        domain="[('company_id', '=', company_id)]")
