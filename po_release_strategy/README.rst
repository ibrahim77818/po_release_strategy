===============================
Purchase Order Release Strategy
===============================

Multi-level, amount-based approval ("release strategy") for Odoo 16
purchase orders. Works on Community and Enterprise, depends only on
``purchase`` and ``mail``.

Configuration
=============

1. *Purchase > Configuration > Settings > Orders*: enable
   **Purchase Release Strategies** (company specific, off by default) and
   optionally pick a **Default Release Strategy**. Once enabled, every
   purchase order of the company needs a release strategy and the native
   "Purchase Order Approval" is no longer used.
2. *Purchase > Configuration > Release Strategies*: create templates.

   * **Approval Rules**: stage (sequence), role, approver type (specific
     user or user group), minimum / maximum amount in company currency
     (maximum 0 = no limit). A rule applies when ``min <= amount <= max``.
     Rules sharing a stage are approved in parallel.
   * **Amount Basis**: total (taxes included) or untaxed amount. Orders in
     a foreign currency are converted to the company currency at the order
     date before rules are evaluated.
   * **After Final Approval**: confirm the order automatically, or put it
     back to RFQ and give the buyer a "Confirm Released Order" activity.
   * **Approval Deadline**: days given to approvers on their activity.
   * **Locked Fields** and **Changes on Confirmed Orders** (see below).
3. Give the *Release Supervisor* right (Settings > Users > Purchase Release
   Strategy) to users allowed to bypass approvals.

Approvers must be internal users with read access to purchase orders
(typically *Purchase / User*). Group members without that access are
skipped; a rule with no eligible approver blocks submission with a clear
error.

Workflow
========

* **Submit for Approval** (or **Confirm Order**) on an RFQ generates the
  approval lines, sets the order to *To Approve* and schedules a
  *Release Approval* activity for each approver of the first stage
  (every eligible member for group rules).
* **Approve** (with optional notes) completes the approver's activity,
  removes the other group members' activities and logs the assignment
  date, approval date, approver and notes. The next stage starts when the
  current stage is fully approved.
* **Reject** (reason required) cancels the remaining lines, returns the
  order to RFQ and notifies the buyer.
* **Reset Release** (buyer, purchase manager or supervisor) clears every
  approval and returns the order to *Not Submitted*.
* **Bypass Approval** (supervisor, reason required) marks the remaining
  lines as bypassed and applies the post-approval action.
* Release activities cannot be marked done or cancelled from the chatter;
  the Approve / Reject buttons keep the log and activities consistent.
* *Purchase > Orders > Release Approvals* lists orders waiting for the
  current user.

Locked fields
=============

A snapshot of the locked fields is stored when the order is submitted.
If any of them changes while the order is waiting for approval or after it
is released, the strategy is reset, previous approvals are cleared, the
approval lines are regenerated and all previous and new approvers are
notified. ``order_line`` locks product, description, quantity, unit of
measure, price and taxes of the lines (received / billed quantities are
not part of the snapshot, so receipts and bills never trigger a reset).

For orders that are already confirmed, choose per strategy whether a
change resets the strategy (the order stays confirmed and a new approval
is requested) or is blocked with an error.

Technical notes
===============

* Models: ``po.release.strategy``, ``po.release.strategy.rule``,
  ``purchase.order.release.line``, wizard ``po.release.action.wizard``.
* ``purchase.order._approval_allowed`` and ``button_approve`` are extended
  so the native flow (including ``purchase_stock`` receipts) only runs
  once the order is released.
* Arabic translation: ``i18n/ar_001.po`` (and ``ar.po``); the custom CSS
  uses logical properties so the layout mirrors in RTL.
* Tests: ``odoo-bin -d <db> -u po_release_strategy --test-enable
  --test-tags /po_release_strategy``.
