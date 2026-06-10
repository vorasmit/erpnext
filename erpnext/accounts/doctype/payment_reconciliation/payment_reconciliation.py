# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# For license information, please see license.txt


import frappe
from frappe import _, msgprint, qb
from frappe.model.document import Document
from frappe.model.meta import get_field_precision
from frappe.query_builder import Criterion
from frappe.query_builder.custom import ConstantColumn
from frappe.utils import flt, get_link_to_form, getdate, nowdate, today

import erpnext
from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import get_dimensions
from erpnext.accounts.doctype.process_payment_reconciliation.process_payment_reconciliation import (
	is_any_doc_running,
)
from erpnext.accounts.utils import (
	CROSS_ACCOUNT_BRIDGE_VOUCHER_TYPE,
	QueryPaymentLedger,
	get_reconciliation_effect_date,
	reconcile_against_document,
)

RECEIVABLE = "Receivable"
PAYABLE = "Payable"


def classify(account_type: str, amount: float) -> str:
	"""Sort a PLE row into Receivable or Payable by `(account_type, sign)`.

	Receivable account + +ve  →  Receivable      Payable account + +ve  →  Payable
	Receivable account + -ve  →  Payable         Payable account + -ve  →  Receivable
	"""
	if account_type not in (RECEIVABLE, PAYABLE):
		frappe.throw(_("Unsupported account type {0}").format(account_type))
	if not amount:
		frappe.throw(_("Cannot classify entry with zero amount."))
	if (amount > 0) == (account_type == RECEIVABLE):
		return RECEIVABLE
	return PAYABLE


def _fifo_key(r):
	"""Sort OpenBalance rows: oldest-first, with PE-Reference rows BEFORE
	their PE-self row (PR-E: drain bound PO/SO advances before free balance)."""
	return (
		r.get("posting_date") or getdate(nowdate()),
		r.get("voucher_no") or "",
		0 if r.get("reference_name") else 1,
		r.get("reference_name") or "",
		r.get("voucher_row") or "",
	)


_WRITABLE_VOUCHER_TYPES = {"Payment Entry", "Journal Entry"}


def link_strategy(recv, pay) -> str:
	"""`voucher_mutation` if either side is PE/JE (append PE.references or
	split JEA); `bridge_je` if both sides are SI/PI/CN/DN (create a system JE).
	"""
	if recv.voucher_type in _WRITABLE_VOUCHER_TYPES or pay.voucher_type in _WRITABLE_VOUCHER_TYPES:
		return "voucher_mutation"
	return "bridge_je"


def pick_voucher_side(recv, pay, party_type=None) -> str:
	"""Return `"receive"` or `"pay"` — the side that owns the `voucher_no`
	slot in `reconcile_against_document`. PE > JE > others.

	For PE-PE pairs, voucher = the advance PE (Receive for Customer, Pay for
	Supplier) to match legacy `add_payment_entries` behaviour. The advance side
	classifies opposite to the party's natural direction: to_pay for Customer,
	to_receive for Supplier. Required for `add_advance_gl_for_reference` to
	emit the bridge GL on the side whose self-PLE row balances the bridge.
	"""
	pe_pair = recv.voucher_type == "Payment Entry" and pay.voucher_type == "Payment Entry"
	if pe_pair and party_type:
		return "pay" if party_type == "Customer" else "receive"
	if pay.voucher_type == "Payment Entry":
		return "pay"
	if recv.voucher_type == "Payment Entry":
		return "receive"
	if pay.voucher_type == "Journal Entry":
		return "pay"
	if recv.voucher_type == "Journal Entry":
		return "receive"
	frappe.throw(_("bridge_je pair without PE/JE: {0} x {1}").format(recv.voucher_type, pay.voucher_type))


class OpenBalanceFetcher:
	"""
	Pipeline:
	1. fetch PLE rows + JE rows for all party accounts
	2. split each PE's PLE row into a free slice + one slice per PO/SO reference
	3. enrich with is_return / is_advance / exchange_rate
	4. filter by amount (min/max + zero-drop)
	"""

	def __init__(self, pr):
		self.pr = pr
		self.company = pr.company
		self.party_type = pr.party_type
		self.party = pr.party
		self.dimensions = pr.dimensions

	def fetch(self):
		accounts = self._get_party_accounts()
		if not accounts:
			return []

		rows = self._query_ple_outstanding(accounts) + self._query_je_outstanding(accounts)
		if not rows:
			return []

		rows = self._split_pe_by_references(rows)
		self._enrich_voucher_metadata(rows)
		return [r for r in rows if self._passes_amount_filter(r)]

	def _get_party_accounts(self):
		"""Single-account when `receivable_payable_account` is set; else all
		party accounts from PLE (split by natural / advance root_type)."""
		accounts = set()
		ple = qb.DocType("Payment Ledger Entry")
		account = qb.DocType("Account")
		rows = (
			qb.from_(ple)
			.inner_join(account)
			.on(ple.account == account.name)
			.select(ple.account)
			.distinct()
			.where(
				(ple.company == self.company)
				& (ple.party_type == self.party_type)
				& (ple.party == self.party)
				& (ple.delinked == 0)
			)
		)

		if self.pr.receivable_payable_account:
			accounts.add(self.pr.receivable_payable_account)
		else:
			natural_root = "Asset" if self.party_type == "Customer" else "Liability"
			normal_accounts = rows.where(account.root_type == natural_root).run(as_dict=True)
			accounts.update([r.account for r in normal_accounts])

		if self.pr.default_advance_account:
			accounts.add(self.pr.default_advance_account)
		else:
			opposite_root = "Liability" if self.party_type == "Customer" else "Asset"
			advance_accounts = rows.where(account.root_type == opposite_root).run(as_dict=True)
			accounts.update([r.account for r in advance_accounts])

		return accounts

	def _build_filters(self, accounts):
		ple = qb.DocType("Payment Ledger Entry")

		common_filter = [
			ple.company == self.company,
			ple.party_type == self.party_type,
			ple.party == self.party,
			ple.account.isin(accounts),
		]
		if self.pr.currency_filter:
			common_filter.append(ple.account_currency == self.pr.currency_filter)

		posting_date_filter = []
		if self.pr.from_date:
			posting_date_filter.append(ple.posting_date.gte(self.pr.from_date))
		if self.pr.to_date:
			posting_date_filter.append(ple.posting_date.lte(self.pr.to_date))

		dimension_filter = []
		for dim in self.dimensions:
			val = self.pr.get(dim.fieldname)
			if val and frappe.db.has_column("Payment Ledger Entry", dim.fieldname):
				dimension_filter.append(ple[dim.fieldname] == val)

		return common_filter, posting_date_filter, dimension_filter

	def _query_ple_outstanding(self, accounts):
		common_filter, posting_date_filter, dimension_filter = self._build_filters(accounts)
		ple = qb.DocType("Payment Ledger Entry")
		common_filter.append(ple.against_voucher_type != "Journal Entry")

		ple_query = QueryPaymentLedger()
		raw = ple_query.get_voucher_outstandings(
			common_filter=common_filter,
			posting_date=posting_date_filter,
			accounting_dimensions=dimension_filter,
			exclude_zero_outstanding=True,
		)
		return [self._project_ple_row(frappe._dict(r)) for r in raw]

	def _project_ple_row(self, r):
		r.outstanding_amount = flt(r.outstanding_in_account_currency)
		r.amount = flt(r.invoice_amount_in_account_currency)
		r.voucher_row = None  # PLE rows are voucher-level, not row-level
		return r

	def _passes_amount_filter(self, r):
		signed = flt(r.get("outstanding_amount"))
		if not signed:
			return False

		abs_out = abs(signed)
		min_amt = self.pr.min_amount or None
		max_amt = self.pr.max_amount or None
		if min_amt and abs_out < min_amt:
			return False
		if max_amt and abs_out > max_amt:
			return False

		return True

	def _split_pe_by_references(self, rows):
		"""Split a PE's single PLE row into a free slice (voucher_row=None) plus
		one bound slice per submitted PO/SO reference (voucher_row=PER.name).

		PLE is authoritative for a PE's total open balance — it nets invoice and
		PE↔PE / JE settlements — but it never records PO/SO references (those live
		only in `Payment Entry Reference`), so it over-states the free balance by
		exactly the bound amount. A bound slice's `voucher_row` lets the Allocator
		pass it as `voucher_detail_no` to `update_reference_in_payment_entry` to
		re-point that advance onto an invoice.
		"""
		pe_names = {r.voucher_no for r in rows if r.voucher_type == "Payment Entry"}
		if not pe_names:
			return rows

		refs_by_pe: dict[str, list] = {}
		for ref in self._fetch_po_so_references(pe_names):
			refs_by_pe.setdefault(ref.pe_name, []).append(ref)

		split_rows = []
		for r in rows:
			refs = refs_by_pe.get(r.voucher_no) if r.voucher_type == "Payment Entry" else None
			if not refs:
				split_rows.append(r)
				continue

			sign = -1 if flt(r.outstanding_amount) < 0 else 1

			bound_total = sum(flt(ref.allocated_amount) for ref in refs)
			free = frappe._dict(r.copy())
			free.outstanding_amount = flt(r.outstanding_amount) - sign * bound_total
			free.amount = free.outstanding_amount
			free.reference_doctype = None
			free.reference_name = None
			split_rows.append(free)

			for ref in refs:
				amt = sign * flt(ref.allocated_amount)
				slice_row = frappe._dict(r.copy())
				slice_row.voucher_row = ref.per_name
				slice_row.outstanding_amount = amt
				slice_row.amount = amt
				slice_row.reference_doctype = ref.reference_doctype
				slice_row.reference_name = ref.reference_name
				split_rows.append(slice_row)

		return split_rows

	def _fetch_po_so_references(self, pe_names):
		"""Submitted PO/SO advance references — the one allocation PLE never records."""
		if not pe_names:
			return []
		per = qb.DocType("Payment Entry Reference")
		return (
			qb.from_(per)
			.select(
				per.name.as_("per_name"),
				per.parent.as_("pe_name"),
				per.allocated_amount,
				per.reference_doctype,
				per.reference_name,
			)
			.where(
				per.parent.isin(list(pe_names))
				& (per.docstatus == 1)
				& per.reference_doctype.isin(("Sales Order", "Purchase Order"))
				& (per.allocated_amount > 0)
			)
			.run(as_dict=True)
		)

	def _query_je_outstanding(self, accounts):
		je = qb.DocType("Journal Entry")
		jea = qb.DocType("Journal Entry Account")
		account_dt = qb.DocType("Account")

		# Sign convention: positive = balance in account's natural direction
		# (Dr-Cr for Receivable accounts, Cr-Dr for Payable).
		if erpnext.get_party_account_type(self.party_type) == "Receivable":
			signed_amount = jea.debit_in_account_currency - jea.credit_in_account_currency
		else:
			signed_amount = jea.credit_in_account_currency - jea.debit_in_account_currency

		conditions = [
			je.docstatus == 1,
			je.company == self.company,
			jea.party_type == self.party_type,
			jea.party == self.party,
			jea.account.isin(accounts),
			(
				(jea.reference_type == "")
				| (jea.reference_type.isnull())
				| (jea.reference_type.isin(("Sales Order", "Purchase Order")))
			),
			signed_amount != 0,
		]
		if self.pr.from_date:
			conditions.append(je.posting_date.gte(self.pr.from_date))
		if self.pr.to_date:
			conditions.append(je.posting_date.lte(self.pr.to_date))
		if self.pr.currency_filter:
			conditions.append(jea.account_currency == self.pr.currency_filter)
		for dim in self.dimensions:
			val = self.pr.get(dim.fieldname)
			if val and frappe.db.has_column("Journal Entry Account", dim.fieldname):
				conditions.append(jea[dim.fieldname] == val)

		query = (
			qb.from_(je)
			.inner_join(jea)
			.on(jea.parent == je.name)
			.inner_join(account_dt)
			.on(account_dt.name == jea.account)
			.select(
				ConstantColumn("Journal Entry").as_("voucher_type"),
				je.name.as_("voucher_no"),
				jea.name.as_("voucher_row"),
				jea.account,
				account_dt.account_type,
				jea.party_type,
				jea.party,
				je.posting_date,
				ConstantColumn(None).as_("due_date"),
				signed_amount.as_("outstanding_amount"),
				signed_amount.as_("amount"),
				jea.account_currency.as_("currency"),
				jea.exchange_rate,
				jea.is_advance,
				ConstantColumn(0).as_("is_return"),
				jea.cost_center,
				je.remark.as_("remarks"),
			)
			.where(Criterion.all(conditions))
			.orderby(je.posting_date)
		)

		raw = query.run(as_dict=True)
		rows = [frappe._dict(r) for r in raw]

		# Net JEA balance against PE-side PLE allocations. A PE settling a JE
		# doesn't touch the JEA (splitting it would drop the JE's self-ref PLE row
		# and break aggregation); instead the PE writes `(voucher=PE, against=JE)`,
		# which nets signed_amount to zero when fully settled.
		if rows:
			je_names = list({r.voucher_no for r in rows})
			ple_dt = qb.DocType("Payment Ledger Entry")
			from frappe.query_builder.functions import Sum

			alloc_rows = (
				qb.from_(ple_dt)
				.select(
					ple_dt.against_voucher_no.as_("je_name"),
					ple_dt.account.as_("account"),
					Sum(ple_dt.amount_in_account_currency).as_("allocated"),
				)
				.where(
					(ple_dt.against_voucher_type == "Journal Entry")
					& ple_dt.against_voucher_no.isin(je_names)
					& (ple_dt.voucher_no != ple_dt.against_voucher_no)  # exclude JE-self
					& (ple_dt.delinked == 0)
					& (ple_dt.company == self.company)
					& (ple_dt.party_type == self.party_type)
					& (ple_dt.party == self.party)
				)
				.groupby(ple_dt.against_voucher_no, ple_dt.account)
				.run(as_dict=True)
			)
			alloc_by_key = {(a.je_name, a.account): flt(a.allocated) for a in alloc_rows}

			# Self-settlement: a JE reconciled against ITSELF (two opposing party
			# legs) writes a split leg referencing the same JE. The base query
			# excludes it (reference_type='Journal Entry') and PLE netting skips it
			# (JE-self), so the settled side would otherwise keep its full value.
			# Net those legs into the base leg here.
			self_settle_rows = (
				qb.from_(jea)
				.inner_join(je)
				.on(jea.parent == je.name)
				.select(jea.parent.as_("je_name"), jea.account, Sum(signed_amount).as_("settled"))
				.where(
					(je.docstatus == 1)
					& jea.parent.isin(je_names)
					& (jea.reference_type == "Journal Entry")
					& (jea.reference_name == jea.parent)
					& (jea.party_type == self.party_type)
					& (jea.party == self.party)
				)
				.groupby(jea.parent, jea.account)
				.run(as_dict=True)
			)
			for s in self_settle_rows:
				key = (s.je_name, s.account)
				alloc_by_key[key] = alloc_by_key.get(key, 0) + flt(s.settled)

			for r in rows:
				alloc = alloc_by_key.get((r.voucher_no, r.account), 0)
				if alloc:
					r.outstanding_amount = flt(r.outstanding_amount) + alloc
					r.amount = r.outstanding_amount

			rows = [r for r in rows if flt(r.outstanding_amount) != 0]

		return rows

	def _enrich_voucher_metadata(self, rows):
		"""Adds is_return, is_advance, and exchange_rate etc. to the PLE rows"""
		by_type: dict[str, set[str]] = {}
		for r in rows:
			if r.voucher_type in ("Sales Invoice", "Purchase Invoice", "Payment Entry"):
				by_type.setdefault(r.voucher_type, set()).add(r.voucher_no)

		invoice_meta: dict[tuple[str, str], tuple[int, float]] = {}
		for vtype in ("Sales Invoice", "Purchase Invoice"):
			if vtype in by_type:
				for v in frappe.db.get_all(
					vtype,
					filters={"name": ("in", list(by_type[vtype]))},
					fields=["name", "is_return", "conversion_rate"],
				):
					invoice_meta[(vtype, v.name)] = (
						int(bool(v.is_return)),
						flt(v.conversion_rate) or 1.0,
					)

		# PE Receive uses source_exchange_rate; PE Pay uses target_exchange_rate.
		pe_meta: dict[str, tuple[int, float]] = {}
		if "Payment Entry" in by_type:
			for p in frappe.db.get_all(
				"Payment Entry",
				filters={"name": ("in", list(by_type["Payment Entry"]))},
				fields=[
					"name",
					"payment_type",
					"book_advance_payments_in_separate_party_account",
					"source_exchange_rate",
					"target_exchange_rate",
				],
			):
				exch = p.source_exchange_rate if p.payment_type == "Receive" else p.target_exchange_rate
				pe_meta[p.name] = (
					int(bool(p.book_advance_payments_in_separate_party_account)),
					flt(exch) or 1.0,
				)

		for r in rows:
			if r.voucher_type in ("Sales Invoice", "Purchase Invoice"):
				is_ret, exch = invoice_meta.get((r.voucher_type, r.voucher_no), (0, 1.0))
				r.is_return = is_ret
				r.is_advance = 0
				r.exchange_rate = exch
			elif r.voucher_type == "Payment Entry":
				if r.voucher_no in pe_meta:
					adv, exch = pe_meta[r.voucher_no]
					r.is_return = 0
					r.is_advance = adv
					r.exchange_rate = exch
			elif r.voucher_type == "Journal Entry":
				continue  # already enriched in _query_je_outstanding
			else:
				r.is_return = 0
				r.is_advance = 0
				r.exchange_rate = 1.0


class Allocator:
	"""Currency-bucketed FIFO allocator.

	Walks `to_receive` oldest-first, consuming `to_pay` oldest-first within
	each currency bucket. Never crosses currencies. Pure transformation —
	`PaymentReconciliation.allocate_entries` clears `self.allocation` and
	appends each emitted dict.
	"""

	def __init__(self, pr, to_receive=None, to_pay=None):
		# `to_receive` / `to_pay` overrides let the form pass user-selected
		# rows (the "select rows then Allocate" UX); defaults to full tables.
		self.pr = pr
		self.company = pr.company
		self.party_type = pr.party_type
		self.to_receive = to_receive if to_receive is not None else list(pr.get("to_receive") or [])
		self.to_pay = to_pay if to_pay is not None else list(pr.get("to_pay") or [])
		self.exchange_gain_loss_account = frappe.get_cached_value(
			"Company", pr.company, "exchange_gain_loss_account"
		)
		self.exc_gain_loss_posting_date_setting = frappe.db.get_single_value(
			"Accounts Settings", "exchange_gain_loss_posting_date", cache=True
		)
		self.company_currency = frappe.get_cached_value("Company", pr.company, "default_currency")
		self.diff_precision = get_field_precision(
			frappe.get_meta("Payment Reconciliation Allocation").get_field("difference_amount")
		)

	def allocate(self):
		buckets_recv = self._bucket_by_currency(self.to_receive)
		buckets_pay = self._bucket_by_currency(self.to_pay)

		allocations = []
		for currency, recv_rows in buckets_recv.items():
			pay_rows = buckets_pay.get(currency, [])
			if not pay_rows:
				continue
			# Shared `_fifo_key` ensures PE-Reference rows drain before PE-self (PR-E).
			recv_rows = sorted(recv_rows, key=_fifo_key)
			pay_rows = sorted(pay_rows, key=_fifo_key)
			allocations.extend(self._walk(recv_rows, pay_rows))

		return allocations

	@staticmethod
	def _bucket_by_currency(rows):
		buckets = {}
		for r in rows:
			buckets.setdefault(r.currency, []).append(r)
		return buckets

	def _walk(self, receivables, payables):
		allocations = []
		recv_remaining = [flt(r.outstanding_amount) for r in receivables]
		pay_remaining = [flt(p.outstanding_amount) for p in payables]

		# Two tiers within the currency bucket: drain SAME-account counterparties
		# first, then fall back to cross-account. This avoids minting a bridge JE
		# when a same-account match exists (cross-account pairs are settled by the
		# transfer-JE bridge in `ReconcileRouter._bridge_cross_account`). FIFO order is
		# preserved within each tier.
		for same_account_only in (True, False):
			for i, recv in enumerate(receivables):
				if recv_remaining[i] <= 0:
					continue
				for j, pay in enumerate(payables):
					if recv_remaining[i] <= 0:
						break
					if pay_remaining[j] <= 0:
						continue
					if (pay.account == recv.account) != same_account_only:
						continue
					amt = min(recv_remaining[i], pay_remaining[j])
					if amt > 0:
						allocations.append(self._make_allocation(recv, payables[j], amt))
						recv_remaining[i] -= amt
						pay_remaining[j] -= amt

		return allocations

	def _make_allocation(self, recv, pay, allocated_amount):
		difference_amount = self._fx_difference(recv, pay, allocated_amount)
		gain_loss_posting_date = self._gain_loss_posting_date(recv, pay)
		is_cross_account = recv.account != pay.account

		# Pick voucher side using the same rule as `ReconcileRouter` — see
		# `pick_voucher_side`. Bridge_je pairs fall back to the party_type
		# convention (CN on to_pay for Customer, DN on to_receive for Supplier)
		# since neither side has a writable refs table.
		if link_strategy(recv, pay) == "voucher_mutation":
			side = pick_voucher_side(recv, pay, self.party_type)
		else:
			side = "pay" if self.party_type == "Customer" else "receive"
		voucher_side_row = pay if side == "pay" else recv
		against_side_row = recv if side == "pay" else pay

		row = frappe._dict(
			{
				"to_receive_voucher_type": recv.voucher_type,
				"to_receive_voucher_no": recv.voucher_no,
				"to_receive_voucher_row": recv.voucher_row,
				"to_receive_account": recv.account,
				"to_receive_party_type": recv.party_type,
				"to_receive_party": recv.party,
				"to_pay_voucher_type": pay.voucher_type,
				"to_pay_voucher_no": pay.voucher_no,
				"to_pay_voucher_row": pay.voucher_row,
				"to_pay_account": pay.account,
				"to_pay_party_type": pay.party_type,
				"to_pay_party": pay.party,
				"allocated_amount": allocated_amount,
				"unreconciled_amount": flt(voucher_side_row.outstanding_amount),
				"amount": flt(voucher_side_row.amount or voucher_side_row.outstanding_amount),
				"is_advance": 1 if (pay.is_advance or recv.is_advance) else 0,
				"is_cross_account": 1 if is_cross_account else 0,
				# `exchange_rate` is the AGAINST side's rate. PE-Reference uses it in
				# `calculate_base_allocated_amount_for_reference` to compute FX gain/loss
				# as (base_rate - ref_rate) x amt; setting it to the voucher-side rate
				# would zero out the gain/loss and skip the FX JE.
				"difference_amount": difference_amount,
				"difference_account": self.exchange_gain_loss_account if difference_amount else None,
				"gain_loss_posting_date": gain_loss_posting_date,
				"exchange_rate": flt(against_side_row.exchange_rate) or 1.0,
				"currency": voucher_side_row.currency,
				# TODO: is opp of voucher side relevant here? Depends on how it's used
				"cost_center": pay.cost_center or recv.cost_center,
			}
		)

		# TODO: evaluate if true - why are these not fetched in the original fetcher?
		# Should it not be updated from original entries?
		for dim in self.pr.dimensions:
			val = self.pr.get(dim.fieldname)
			if val:
				row[dim.fieldname] = val

		return row

	def _fx_difference(self, recv, pay, allocated_amount):
		"""FX gain/loss when the two sides booked at different exchange rates.

		Sign convention matches `Payment Entry Reference.exchange_gain_loss` so
		`make_exchange_gain_loss_journal` produces correct JEs. Phase 3 PRE will
		replace this with `amount x (received_rate - paid_rate)` once both rates
		live on a single PRE row.
		"""
		recv_rate = flt(recv.exchange_rate) or 1.0
		pay_rate = flt(pay.exchange_rate) or 1.0
		if recv_rate == pay_rate:
			return 0.0
		if recv.currency == self.company_currency and pay.currency == self.company_currency:
			return 0.0

		amt_in_pay_rate = flt(pay_rate * flt(allocated_amount), self.diff_precision)
		amt_in_recv_rate = flt(recv_rate * flt(allocated_amount), self.diff_precision)

		# Cash-event pair (PExPE, JExE, PExJE): neither side is the "booked"
		# asset/liability so use the Receivable-style formula for both parties.
		invoice_doctypes = ("Sales Invoice", "Purchase Invoice")
		if recv.voucher_type not in invoice_doctypes and pay.voucher_type not in invoice_doctypes:
			return amt_in_pay_rate - amt_in_recv_rate

		# Standard SI/PI ↔ PE: invert sign for Payable accounts (Supplier).
		if recv.account_type == PAYABLE:
			return amt_in_recv_rate - amt_in_pay_rate
		return amt_in_pay_rate - amt_in_recv_rate

	def _gain_loss_posting_date(self, recv, pay):
		date = pay.posting_date
		# TODO: should this be based on the voucher side instead?
		if pay.is_advance:
			return date
		if self.exc_gain_loss_posting_date_setting == "Invoice":
			date = recv.posting_date
		elif self.exc_gain_loss_posting_date_setting == "Reconciliation Date":
			date = nowdate()
		return date


class ReconcileRouter:
	"""Per-Allocation-row dispatch. Every pair is settled by mutating a writable
	voucher via `reconcile_against_document`. A pair with no writable voucher in a
	shared account (cross-account, or same-account invoice↔note) is first
	re-expressed by `_bridge_cross_account` as two rows against a minted bridge JE.
	"""

	def __init__(self, pr):
		self.pr = pr
		self.company = pr.company
		self.party_type = pr.party_type
		self.party_account_type = erpnext.get_party_account_type(pr.party_type)
		self.dr_or_cr = (
			"credit_in_account_currency"
			if self.party_account_type == RECEIVABLE
			else "debit_in_account_currency"
		)

	def execute(self, skip_ref_details_update_for_pe=False):
		adjust_allocations_for_taxes(self.pr)

		standard_args = []

		for row in self.pr.get("allocation") or []:
			if not (row.allocated_amount and row.to_receive_voucher_no and row.to_pay_voucher_no):
				continue

			sub_rows = self._bridge_cross_account(row) if self._needs_bridge(row) else [row]

			# Post-bridge, every row has a writable voucher, so all go through
			# reconcile_against_document (no separate invoice↔note path anymore).
			for r in sub_rows:
				standard_args.append(self._build_payment_args(r))

		if standard_args:
			reconcile_against_document(standard_args, skip_ref_details_update_for_pe, self.pr.dimensions)

	def _bridge_cross_account(self, row):
		"""Re-express the pair as two same-account rows against a minted transfer JE,
		each pairing an original voucher with the matching bridge leg. The JE split
		runs as update-after-submit, so `validate_against_jv` never blocks it.

		FX rides on the to_pay sub-row (the transfer legs are rate-matched), so
		`reconcile_against_document` books it via `make_exchange_gain_loss_journal`.
		"""
		bridge = _post_cross_account_transfer(row, self.company, self.pr.dimensions)

		# accounts[0] = credit leg (to_receive account); accounts[1] = debit leg (to_pay account).
		recv_leg, pay_leg = bridge.accounts[0], bridge.accounts[1]

		row_r = frappe._dict(row.as_dict())
		row_r.update(
			{
				"to_pay_voucher_type": "Journal Entry",
				"to_pay_voucher_no": bridge.name,
				"to_pay_voucher_row": recv_leg.name,
				"to_pay_account": row.to_receive_account,
				"to_pay_party_type": row.to_receive_party_type,
				"to_pay_party": row.to_receive_party,
				"difference_amount": 0,
				"difference_account": None,
			}
		)

		# Keeps difference_amount / difference_account so the FX JE is booked here.
		row_p = frappe._dict(row.as_dict())
		row_p.update(
			{
				"to_receive_voucher_type": "Journal Entry",
				"to_receive_voucher_no": bridge.name,
				"to_receive_voucher_row": pay_leg.name,
				"to_receive_account": row.to_pay_account,
				"to_receive_party_type": row.to_pay_party_type,
				"to_receive_party": row.to_pay_party,
			}
		)

		return [row_r, row_p]

	def _needs_bridge(self, row):
		"""Bridge when no writable voucher in a shared account can be mutated to settle
		both sides: a same-account pair where NEITHER side is writable (invoice↔note),
		or any cross-account pair. The exception below is a PE booking its advance in a
		separate party account — already bridged natively by `make_advance_gl_entries`,
		so bridging it again would double-handle it.
		"""
		if row.to_receive_account == row.to_pay_account:
			recv_writable = row.to_receive_voucher_type in _WRITABLE_VOUCHER_TYPES
			pay_writable = row.to_pay_voucher_type in _WRITABLE_VOUCHER_TYPES
			return not (recv_writable or pay_writable)

		for vtype, vno in (
			(row.to_receive_voucher_type, row.to_receive_voucher_no),
			(row.to_pay_voucher_type, row.to_pay_voucher_no),
		):
			if vtype == "Payment Entry" and frappe.db.get_value(
				"Payment Entry", vno, "book_advance_payments_in_separate_party_account"
			):
				return False

		return True

	def _build_payment_args(self, row):
		"""Build `reconcile_against_document` args. Every row here has a writable
		voucher (original or bridge JE leg), so `pick_voucher_side` always resolves.
		"""
		recv = frappe._dict(voucher_type=row.to_receive_voucher_type)
		pay = frappe._dict(voucher_type=row.to_pay_voucher_type)

		side = pick_voucher_side(recv, pay, self.party_type)

		voucher_side = "to_pay" if side == "pay" else "to_receive"
		against_side = "to_receive" if side == "pay" else "to_pay"

		voucher_type = row.get(f"{voucher_side}_voucher_type")
		voucher_account = row.get(f"{voucher_side}_account")
		against_account = row.get(f"{against_side}_account")

		if voucher_type == "Journal Entry":
			account = voucher_account
			dr_or_cr = (
				"debit_in_account_currency" if voucher_side == "to_receive" else "credit_in_account_currency"
			)
		else:
			account = against_account
			dr_or_cr = self.dr_or_cr

		args = frappe._dict(
			{
				"voucher_type": voucher_type,
				"voucher_no": row.get(f"{voucher_side}_voucher_no"),
				"voucher_detail_no": row.get(f"{voucher_side}_voucher_row"),
				"against_voucher_type": row.get(f"{against_side}_voucher_type"),
				"against_voucher": row.get(f"{against_side}_voucher_no"),
				"account": account or self.pr.receivable_payable_account,
				"exchange_rate": row.get("exchange_rate"),
				"party_type": row.get(f"{voucher_side}_party_type") or self.party_type,
				"party": row.get(f"{voucher_side}_party") or self.pr.party,
				"is_advance": row.get("is_advance"),
				"dr_or_cr": dr_or_cr,
				"unreconciled_amount": flt(row.get("unreconciled_amount")),
				"unadjusted_amount": flt(row.get("amount")),
				"allocated_amount": flt(row.get("allocated_amount")),
				"difference_amount": flt(row.get("difference_amount")),
				"difference_account": row.get("difference_account"),
				"difference_posting_date": row.get("gain_loss_posting_date"),
				"cost_center": row.get("cost_center"),
				"currency": row.get("currency"),
			}
		)

		for dim in self.pr.dimensions:
			if row.get(dim.fieldname):
				args[dim.fieldname] = row.get(dim.fieldname)

		return args


class PaymentReconciliation(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.accounts.doctype.payment_reconciliation_allocation.payment_reconciliation_allocation import (
			PaymentReconciliationAllocation,
		)
		from erpnext.accounts.doctype.payment_reconciliation_entry.payment_reconciliation_entry import (
			PaymentReconciliationEntry,
		)

		allocation: DF.Table[PaymentReconciliationAllocation]
		company: DF.Link
		cost_center: DF.Link | None
		currency_filter: DF.Link | None
		default_advance_account: DF.Link | None
		from_date: DF.Date | None
		max_amount: DF.Currency
		min_amount: DF.Currency
		party: DF.DynamicLink
		fetch_limit: DF.Int
		party_type: DF.Link
		project: DF.Link | None
		receivable_payable_account: DF.Link | None
		to_date: DF.Date | None
		to_pay: DF.Table[PaymentReconciliationEntry]
		to_receive: DF.Table[PaymentReconciliationEntry]
	# end: auto-generated types

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.common_filter_conditions = []
		self.accounting_dimension_filter_conditions = []
		self.ple_posting_date_filter = []
		# `dimensions` is the list of configured accounting-dimension DEFINITIONS
		# (cost_center + project + any custom dimensions). It is NOT the values — the
		# per-dimension filter VALUES are read on demand from the PR's own filter
		# fields via `self.get(dim.fieldname)` (these fields exist on the Payment
		# Reconciliation doctype as filters the user sets on the form).
		self.dimensions = get_dimensions(with_cost_center_and_project=True)[0]

	def load_from_db(self):
		# 'modified' attribute is required for `run_doc_method` to work properly.
		doc_dict = frappe._dict(
			{
				"modified": None,
				"company": None,
				"party": None,
				"party_type": None,
				"receivable_payable_account": None,
				"default_advance_account": None,
				"currency_filter": None,
				"from_date": None,
				"to_date": None,
				"min_amount": None,
				"max_amount": None,
				"fetch_limit": 50,
				"cost_center": None,
				"project": None,
			}
		)
		super(Document, self).__init__(doc_dict)

	def save(self):
		return

	@staticmethod
	def get_list(args):
		pass

	@staticmethod
	def get_count(args):
		pass

	@staticmethod
	def get_stats(args):
		pass

	def db_insert(self, *args, **kwargs):
		pass

	def db_update(self, *args, **kwargs):
		pass

	def delete(self):
		pass

	@frappe.whitelist()
	def get_unreconciled_entries(self):
		# Pipeline: fetch → classify (Receivable/Payable) → cap per `fetch_limit`.
		self.set("to_receive", [])
		self.set("to_pay", [])
		self.check_mandatory_to_fetch()

		open_balances = OpenBalanceFetcher(self).fetch()
		self._classify_and_populate(open_balances)
		self._apply_fetch_limit()

	def _classify_and_populate(self, open_balances):
		open_balances.sort(key=_fifo_key)

		for row in open_balances:
			signed = flt(row.get("outstanding_amount"))
			if not signed:
				continue
			target = classify(row.get("account_type"), signed)
			table = "to_receive" if target == RECEIVABLE else "to_pay"

			self.append(
				table,
				{
					**row,
					"amount": abs(signed),
					"outstanding_amount": abs(signed),
					"exchange_rate": flt(row.get("exchange_rate")) or 1.0,
				},
			)

	def _apply_fetch_limit(self):
		limit = self.fetch_limit
		if limit and len(self.to_receive) > limit:
			self.to_receive = self.to_receive[:limit]
		if limit and len(self.to_pay) > limit:
			self.to_pay = self.to_pay[:limit]

	@frappe.whitelist()
	def is_auto_process_enabled(self):
		return frappe.get_single_value("Accounts Settings", "auto_reconcile_payments")

	@frappe.whitelist()
	def calculate_difference_on_allocation_change(
		self,
		payment_entry: list | None = None,
		invoice: list | None = None,
		allocated_amount: float | None = None,
	):
		"""Backward-compat API for external callers that manually edit an
		allocation row's allocated_amount. Delegates to `Allocator._fx_difference`."""
		if not (payment_entry and invoice):
			return 0.0
		pay = (
			frappe._dict(payment_entry[0]) if isinstance(payment_entry, list) else frappe._dict(payment_entry)
		)
		recv = frappe._dict(invoice[0]) if isinstance(invoice, list) else frappe._dict(invoice)
		return Allocator(self)._fx_difference(recv, pay, flt(allocated_amount))

	@frappe.whitelist()
	def allocate_entries(
		self,
		args: dict | str | None = None,
		to_receive: list | None = None,
		to_pay: list | None = None,
		**kwargs,
	):
		"""Run the Allocator. Three call shapes:
		1. `({"to_receive": [...], "to_pay": [...]})` — Process PR background job
		2. `(to_receive=[...], to_pay=[...])` — form's "select rows then Allocate"
		3. `()` — Auto-Match across the full parent tables
		"""
		if isinstance(args, str):
			import json as _json

			args = _json.loads(args)
		if args is None:
			args = {}
		to_receive = to_receive if to_receive is not None else args.get("to_receive")
		to_pay = to_pay if to_pay is not None else args.get("to_pay")

		if to_receive is not None:
			to_receive = [frappe._dict(r) if isinstance(r, dict) else r for r in to_receive]
		if to_pay is not None:
			to_pay = [frappe._dict(r) if isinstance(r, dict) else r for r in to_pay]

		if not (to_receive or self.get("to_receive")):
			frappe.throw(_("No records found in the To Receive table"))
		if not (to_pay or self.get("to_pay")):
			frappe.throw(_("No records found in the To Pay table"))

		allocations = Allocator(self, to_receive=to_receive, to_pay=to_pay).allocate()

		self.set("allocation", [])
		for entry in allocations:
			if entry["allocated_amount"]:
				row = self.append("allocation", {})
				row.update(entry)

	@frappe.whitelist()
	def reconcile(self):
		if frappe.get_single_value("Accounts Settings", "auto_reconcile_payments"):
			running_doc = is_any_doc_running(
				dict(
					company=self.company,
					party_type=self.party_type,
					party=self.party,
					receivable_payable_account=self.receivable_payable_account,
				)
			)
			if running_doc:
				frappe.throw(
					_(
						"A Reconciliation Job {0} is running for the same filters. Cannot reconcile now"
					).format(get_link_to_form("Auto Reconcile", running_doc))
				)
				return

		self.validate_allocation()
		ReconcileRouter(self).execute()
		msgprint(_("Successfully Reconciled"))

		self.get_unreconciled_entries()

	def validate_allocation(self):
		"""Per-row checks: both sides set, allocated ≤ each side's outstanding,
		matching currency, matching party. Cross-currency / cross-party are
		Phase 1 limits — Allocator already prevents the former; this guards
		manual edits and future cross-party flows.
		"""
		recv_index = {
			(r.voucher_type, r.voucher_no, r.voucher_row or ""): r for r in self.get("to_receive") or []
		}
		pay_index = {(r.voucher_type, r.voucher_no, r.voucher_row or ""): r for r in self.get("to_pay") or []}

		any_valid = False
		for row in self.get("allocation") or []:
			if not (row.allocated_amount and row.to_receive_voucher_no and row.to_pay_voucher_no):
				continue
			any_valid = True

			recv_key = (
				row.to_receive_voucher_type,
				row.to_receive_voucher_no,
				row.to_receive_voucher_row or "",
			)
			pay_key = (row.to_pay_voucher_type, row.to_pay_voucher_no, row.to_pay_voucher_row or "")

			recv_src = recv_index.get(recv_key)
			pay_src = pay_index.get(pay_key)
			recv_outs = flt(recv_src.outstanding_amount) if recv_src else 0
			pay_outs = flt(pay_src.outstanding_amount) if pay_src else 0
			allocated = flt(row.allocated_amount)

			if recv_outs and allocated - recv_outs > 0.009:
				frappe.throw(
					_("Row {0}: Allocated amount {1} exceeds receivable outstanding {2} for {3}").format(
						row.idx, allocated, recv_outs, row.to_receive_voucher_no
					)
				)
			if pay_outs and allocated - pay_outs > 0.009:
				frappe.throw(
					_("Row {0}: Allocated amount {1} exceeds payable outstanding {2} for {3}").format(
						row.idx, allocated, pay_outs, row.to_pay_voucher_no
					)
				)

			if recv_src and pay_src and recv_src.currency and pay_src.currency:
				if recv_src.currency != pay_src.currency:
					frappe.throw(
						_(
							"Row {0}: Cross-currency reconciliation is not supported. "
							"Receivable {1} ({2}) does not match payable {3} ({4})."
						).format(
							row.idx,
							row.to_receive_voucher_no,
							recv_src.currency,
							row.to_pay_voucher_no,
							pay_src.currency,
						)
					)

			if (
				row.to_receive_party_type
				and row.to_pay_party_type
				and (
					row.to_receive_party_type != row.to_pay_party_type
					or row.to_receive_party != row.to_pay_party
				)
			):
				frappe.throw(
					_(
						"Row {0}: Cross-party reconciliation is not supported in this phase ({1} {2} vs {3} {4})"
					).format(
						row.idx,
						row.to_receive_party_type,
						row.to_receive_party,
						row.to_pay_party_type,
						row.to_pay_party,
					)
				)

		if not any_valid:
			frappe.throw(_("No records found in Allocation table"))

	def check_mandatory_to_fetch(self):
		for fieldname in ["company", "party_type", "party"]:
			if not self.get(fieldname):
				frappe.throw(_("Please select {0} first").format(_(self.meta.get_label(fieldname))))


def _make_system_journal(**kwargs):
	"""Create + submit a system-generated JE from kwargs (fields and `doc.flags.*`),
	falling back to standard system values. Used by `_post_cross_account_transfer`.
	"""
	jv = frappe.get_doc(
		{
			"doctype": "Journal Entry",
			"voucher_type": kwargs.get("voucher_type"),
			"company": kwargs.get("company"),
			"posting_date": kwargs.get("posting_date") or today(),
			"multi_currency": kwargs.get("multi_currency", 0),
			"accounts": kwargs.get("accounts"),
		}
	)
	jv.flags.ignore_mandatory = kwargs.get("ignore_mandatory", True)
	jv.flags.ignore_exchange_rate = kwargs.get("ignore_exchange_rate", True)
	jv.flags.skip_remarks_creation = kwargs.get("skip_remarks_creation", True)
	jv.is_system_generated = kwargs.get("is_system_generated", True)
	jv.remark = kwargs.get("remark", None)
	jv.submit()
	return jv


def _post_cross_account_transfer(row, company, active_dimensions=None):
	"""Post + submit the no-reference bridge transfer JE (same party, both legs).
	Cross-account: a genuine transfer between the two accounts. Same-account
	invoice↔note: both legs net to zero; the settling is done by the later JEA splits.
	"""
	amount = abs(flt(row.allocated_amount))
	cost_center = row.cost_center or erpnext.get_default_cost_center(company)
	company_currency = erpnext.get_company_currency(company)
	exchange_rate = flt(row.exchange_rate) or 1.0

	def leg(account, party_type, party, dr_or_cr):
		entry = frappe._dict(
			{
				"account": account,
				"party_type": party_type,
				"party": party,
				"cost_center": cost_center,
				"exchange_rate": exchange_rate,
				dr_or_cr: amount,
			}
		)
		if active_dimensions:
			for dim in active_dimensions:
				if row.get(dim.fieldname):
					entry[dim.fieldname] = row.get(dim.fieldname)
		return entry

	accounts = [
		# to_receive voucher carries a debit balance → credit its account.
		leg(
			row.to_receive_account,
			row.to_receive_party_type,
			row.to_receive_party,
			"credit_in_account_currency",
		),
		# to_pay voucher carries a credit balance → debit its account.
		leg(row.to_pay_account, row.to_pay_party_type, row.to_pay_party, "debit_in_account_currency"),
	]

	# Posting date follows `reconciliation_takes_effect_on`: the SI/PI is the
	# "invoice", the other side supplies the pay date (no invoice → to_receive stands in).
	invoice_types = ("Sales Invoice", "Purchase Invoice")
	if row.to_pay_voucher_type in invoice_types:
		invoice_type, invoice_no = row.to_pay_voucher_type, row.to_pay_voucher_no
		pay_type, pay_no = row.to_receive_voucher_type, row.to_receive_voucher_no
	else:
		invoice_type, invoice_no = row.to_receive_voucher_type, row.to_receive_voucher_no
		pay_type, pay_no = row.to_pay_voucher_type, row.to_pay_voucher_no
	pay_date = frappe.db.get_value(pay_type, pay_no, "posting_date") or today()
	posting_date = get_reconciliation_effect_date(invoice_type, invoice_no, company, pay_date)

	return _make_system_journal(
		company=company,
		voucher_type=CROSS_ACCOUNT_BRIDGE_VOUCHER_TYPE,
		posting_date=posting_date,
		multi_currency=1 if row.currency != company_currency else 0,
		accounts=accounts,
	)


@erpnext.allow_regional
def adjust_allocations_for_taxes(doc):
	pass


@frappe.whitelist()
def get_queries_for_dimension_filters(company: str | None = None):
	dimensions_with_filters = []
	for d in get_dimensions()[0]:
		filters = {}
		meta = frappe.get_meta(d.document_type)
		if meta.has_field("company") and company:
			filters.update({"company": company})

		if meta.is_tree:
			filters.update({"is_group": 0})

		dimensions_with_filters.append({"fieldname": d.fieldname, "filters": filters})

	return dimensions_with_filters
