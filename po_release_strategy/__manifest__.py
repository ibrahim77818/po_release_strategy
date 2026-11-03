# -*- coding: utf-8 -*-
{
    'name': 'Purchase Order Release Strategy',
    'version': '16.0.1.0.0',
    'category': 'Inventory/Purchase',
    'summary': 'Multi-level purchase order approvals: amount-based release '
               'strategies, user/group approvers, activities, supervisor '
               'bypass and post-approval field locking.',
    'description': """
Purchase Order Release Strategy
===============================
* Company-level switch in Purchase settings that makes a release strategy
  mandatory for every purchase order.
* Release strategy templates with sequenced approval rules (specific user or
  user group, role, min/max amount in company currency).
* Foreign-currency orders are normalized to the company currency before the
  rules are evaluated.
* Approval lines and mail activities are generated automatically, stage by
  stage; approval logs assignment/approval timestamps, approver and notes.
* Reject, manual reset and supervisor bypass.
* Post-approval behaviour: confirm automatically or notify the buyer.
* Locked fields: modifying them after submission resets the strategy, clears
  approvals and re-notifies every approver.
* Arabic (ar_001) translation, RTL ready.
""",
    'author': 'Ibrahim Yahia',
    'website': 'https://www.linkedin.com/in/ibrahim-yahia',
    'license': 'LGPL-3',
    'depends': ['purchase', 'mail'],
    'data': [
        'security/po_release_security.xml',
        'security/ir.model.access.csv',
        'data/mail_activity_type_data.xml',
        'wizard/po_release_action_wizard_views.xml',
        'views/po_release_strategy_views.xml',
        'views/purchase_order_views.xml',
        'views/res_config_settings_views.xml',
        'views/menus.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'po_release_strategy/static/src/css/po_release_strategy.css',
        ],
    },
    'images': ['static/description/banner.png'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
