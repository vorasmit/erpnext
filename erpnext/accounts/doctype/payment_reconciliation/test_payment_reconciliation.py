# Copyright (c) 2021, Frappe Technologies Pvt. Ltd. and Contributors
# See license.txt


import unittest

import frappe
from frappe import qb
from frappe.utils import add_days, add_years, flt, getdate, nowdate, today
from frappe.utils.data import getdate as convert_to_date

from erpnext import get_default_cost_center
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
from erpnext.accounts.doctype.payment_entry.test_payment_entry import create_payment_entry
from erpnext.accounts.doctype.payment_reconciliation.payment_reconciliation import classify
from erpnext.accounts.doctype.purchase_invoice.test_purchase_invoice import make_purchase_invoice
from erpnext.accounts.doctype.sales_invoice.test_sales_invoice import create_sales_invoice
from erpnext.accounts.party import get_party_account
from erpnext.accounts.utils import get_fiscal_year
from erpnext.buying.doctype.purchase_order.test_purchase_order import create_purchase_order
from erpnext.stock.doctype.item.test_item import create_item
from erpnext.tests.utils import ERPNextTestSuite


class TestPaymentReconciliation(ERPNextTestSuite):
	def setUp(self):
		self.create_company()
		self.create_item()
		self.create_customer()
		self.create_account()
		self.create_cost_center()
		self.clear_old_entries()

	def create_company(self):
		company = None
		if frappe.db.exists("Company", "_Test Payment Reconciliation"):
			company = frappe.get_doc("Company", "_Test Payment Reconciliation")
		else:
			company = frappe.get_doc(
				{
					"doctype": "Company",
					"company_name": "_Test Payment Reconciliation",
					"country": "India",
					"default_currency": "INR",
					"create_chart_of_accounts_based_on": "Standard Template",
					"chart_of_accounts": "Standard",
				}
			)
			company = company.save()

		self.company = company.name
		self.cost_center = company.cost_center
		self.warehouse = "All Warehouses - _PR"
		self.income_account = "Sales - _PR"
		self.expense_account = "Cost of Goods Sold - _PR"
		self.debit_to = "Debtors - _PR"
		self.creditors = "Creditors - _PR"
		self.cash = "Cash - _PR"

		# create bank account
		if frappe.db.exists("Account", "HDFC - _PR"):
			self.bank = "HDFC - _PR"
		else:
			bank_acc = frappe.get_doc(
				{
					"doctype": "Account",
					"account_name": "HDFC",
					"parent_account": "Bank Accounts - _PR",
					"company": self.company,
				}
			)
			bank_acc.save()
			self.bank = bank_acc.name

	def create_item(self):
		item = create_item(
			item_code="_Test PR Item", is_stock_item=0, company=self.company, warehouse=self.warehouse
		)
		self.item = item if isinstance(item, str) else item.item_code

	def create_customer(self):
		self.customer = make_customer("_Test PR Customer")
		self.customer2 = make_customer("_Test PR Customer 2")
		self.customer3 = make_customer("_Test PR Customer 3", "EUR")
		self.customer4 = make_customer("_Test PR Customer 4", "EUR")
		self.customer5 = make_customer("_Test PR Customer 5", "EUR")

	def create_account(self):
		accounts = [
			{
				"attribute": "debtors_eur",
				"account_name": "Debtors EUR",
				"parent_account": "Accounts Receivable - _PR",
				"account_currency": "EUR",
				"account_type": "Receivable",
			},
			{
				"attribute": "creditors_usd",
				"account_name": "Payable USD",
				"parent_account": "Accounts Payable - _PR",
				"account_currency": "USD",
				"account_type": "Payable",
			},
			# 'Payable' account for capturing advance paid, under 'Assets' group
			{
				"attribute": "advance_payable_account",
				"account_name": "Advance Paid",
				"parent_account": "Current Assets - _PR",
				"account_currency": "INR",
				"account_type": "Payable",
			},
			# 'Receivable' account for capturing advance received, under 'Liabilities' group
			{
				"attribute": "advance_receivable_account",
				"account_name": "Advance Received",
				"parent_account": "Current Liabilities - _PR",
				"account_currency": "INR",
				"account_type": "Receivable",
			},
		]

		for x in accounts:
			x = frappe._dict(x)
			if not frappe.db.get_value(
				"Account", filters={"account_name": x.account_name, "company": self.company}
			):
				acc = frappe.new_doc("Account")
				acc.account_name = x.account_name
				acc.parent_account = x.parent_account
				acc.company = self.company
				acc.account_currency = x.account_currency
				acc.account_type = x.account_type
				acc.insert()
			else:
				name = frappe.db.get_value(
					"Account",
					filters={"account_name": x.account_name, "company": self.company},
					fieldname="name",
					pluck=True,
				)
				acc = frappe.get_doc("Account", name)
			setattr(self, x.attribute, acc.name)

	def create_sales_invoice(
		self, qty=1, rate=100, posting_date=None, do_not_save=False, do_not_submit=False
	):
		"""
		Helper function to populate default values in sales invoice
		"""
		if posting_date is None:
			posting_date = nowdate()

		sinv = create_sales_invoice(
			qty=qty,
			rate=rate,
			company=self.company,
			customer=self.customer,
			item_code=self.item,
			item_name=self.item,
			cost_center=self.cost_center,
			warehouse=self.warehouse,
			debit_to=self.debit_to,
			parent_cost_center=self.cost_center,
			update_stock=0,
			currency="INR",
			is_pos=0,
			is_return=0,
			return_against=None,
			income_account=self.income_account,
			expense_account=self.expense_account,
			do_not_save=do_not_save,
			do_not_submit=do_not_submit,
		)
		return sinv

	def create_payment_entry(self, amount=100, posting_date=None, customer=None):
		"""
		Helper function to populate default values in payment entry
		"""
		if posting_date is None:
			posting_date = nowdate()

		payment = create_payment_entry(
			company=self.company,
			payment_type="Receive",
			party_type="Customer",
			party=customer or self.customer,
			paid_from=self.debit_to,
			paid_to=self.bank,
			paid_amount=amount,
		)
		payment.posting_date = posting_date
		return payment

	def create_purchase_invoice(
		self, qty=1, rate=100, posting_date=None, do_not_save=False, do_not_submit=False
	):
		"""
		Helper function to populate default values in sales invoice
		"""
		if posting_date is None:
			posting_date = nowdate()

		pinv = make_purchase_invoice(
			qty=qty,
			rate=rate,
			company=self.company,
			customer=self.supplier,
			item_code=self.item,
			item_name=self.item,
			cost_center=self.cost_center,
			warehouse=self.warehouse,
			debit_to=self.debit_to,
			parent_cost_center=self.cost_center,
			update_stock=0,
			currency="INR",
			is_pos=0,
			is_return=0,
			return_against=None,
			income_account=self.income_account,
			expense_account=self.expense_account,
			do_not_save=do_not_save,
			do_not_submit=do_not_submit,
		)
		return pinv

	def create_purchase_order(
		self, qty=1, rate=100, posting_date=None, do_not_save=False, do_not_submit=False
	):
		"""
		Helper function to populate default values in sales invoice
		"""
		if posting_date is None:
			posting_date = nowdate()

		pord = create_purchase_order(
			qty=qty,
			rate=rate,
			company=self.company,
			customer=self.supplier,
			item_code=self.item,
			item_name=self.item,
			cost_center=self.cost_center,
			warehouse=self.warehouse,
			debit_to=self.debit_to,
			parent_cost_center=self.cost_center,
			update_stock=0,
			currency="INR",
			is_pos=0,
			is_return=0,
			return_against=None,
			income_account=self.income_account,
			expense_account=self.expense_account,
			do_not_save=do_not_save,
			do_not_submit=do_not_submit,
		)
		return pord

	def clear_old_entries(self):
		doctype_list = [
			"GL Entry",
			"Payment Ledger Entry",
			"Sales Invoice",
			"Purchase Invoice",
			"Payment Entry",
			"Journal Entry",
		]
		for doctype in doctype_list:
			qb.from_(qb.DocType(doctype)).delete().where(qb.DocType(doctype).company == self.company).run()

	def create_payment_reconciliation(self, party_is_customer=True):
		pr = frappe.new_doc("Payment Reconciliation")
		pr.company = self.company
		pr.party_type = "Customer" if party_is_customer else "Supplier"
		pr.party = self.customer if party_is_customer else self.supplier
		pr.receivable_payable_account = get_party_account(pr.party_type, pr.party, pr.company)
		pr.from_date = pr.to_date = nowdate()
		return pr

	def create_journal_entry(self, acc1=None, acc2=None, amount=0, posting_date=None, cost_center=None):
		je = frappe.new_doc("Journal Entry")
		je.posting_date = posting_date or nowdate()
		je.company = self.company
		je.user_remark = "test"
		if not cost_center:
			cost_center = self.cost_center
		je.set(
			"accounts",
			[
				{
					"account": acc1,
					"cost_center": cost_center,
					"debit_in_account_currency": amount if amount > 0 else 0,
					"credit_in_account_currency": abs(amount) if amount < 0 else 0,
				},
				{
					"account": acc2,
					"cost_center": cost_center,
					"credit_in_account_currency": amount if amount > 0 else 0,
					"debit_in_account_currency": abs(amount) if amount < 0 else 0,
				},
			],
		)
		return je

	def create_cost_center(self):
		# Setup cost center
		cc_name = "Sub"

		self.main_cc = frappe.get_doc("Cost Center", get_default_cost_center(self.company))

		cc_exists = frappe.db.get_list("Cost Center", filters={"cost_center_name": cc_name})
		if cc_exists:
			self.sub_cc = frappe.get_doc("Cost Center", cc_exists[0].name)
		else:
			sub_cc = frappe.new_doc("Cost Center")
			sub_cc.cost_center_name = "Sub"
			sub_cc.parent_cost_center = self.main_cc.parent_cost_center
			sub_cc.company = self.main_cc.company
			self.sub_cc = sub_cc.save()

	def test_filter_min_max(self):
		# unified min_amount/max_amount filter applied to both to_receive and to_pay.
		self.create_sales_invoice(qty=1, rate=300)
		self.create_sales_invoice(qty=1, rate=400)
		self.create_sales_invoice(qty=1, rate=500)
		self.create_payment_entry(amount=300).save().submit()
		self.create_payment_entry(amount=400).save().submit()
		self.create_payment_entry(amount=500).save().submit()

		pr = self.create_payment_reconciliation()
		# Range 400-500 → 2 SIs and 2 PEs in that band.
		pr.min_amount = 400
		pr.max_amount = 500
		pr.get_unreconciled_entries()
		self.assertEqual(len(pr.get("to_receive")), 2)
		self.assertEqual(len(pr.get("to_pay")), 2)

		# Single point 400 → 1 SI, 1 PE.
		pr.min_amount = pr.max_amount = 400
		pr.get_unreconciled_entries()
		self.assertEqual(len(pr.get("to_receive")), 1)
		self.assertEqual(len(pr.get("to_pay")), 1)

		# Cleared filter → all 3 each side.
		pr.min_amount = pr.max_amount = 0
		pr.get_unreconciled_entries()
		self.assertEqual(len(pr.get("to_receive")), 3)
		self.assertEqual(len(pr.get("to_pay")), 3)

	def test_filter_posting_date(self):
		# check filter condition using transaction date
		date1 = nowdate()
		date2 = add_days(nowdate(), -1)
		amount = 100
		self.create_sales_invoice(qty=1, rate=amount, posting_date=date1)
		si2 = self.create_sales_invoice(
			qty=1, rate=amount, posting_date=date2, do_not_save=True, do_not_submit=True
		)
		si2.set_posting_time = 1
		si2.posting_date = date2
		si2.save().submit()
		self.create_payment_entry(amount=amount, posting_date=date1).save().submit()
		self.create_payment_entry(amount=amount, posting_date=date2).save().submit()

		pr = self.create_payment_reconciliation()
		pr.from_date = pr.to_date = date1

		pr.get_unreconciled_entries()
		# assert only si and pe are fetched
		self.assertEqual(len(pr.get("to_receive")), 1)
		self.assertEqual(len(pr.get("to_pay")), 1)

		pr.from_date = date2
		pr.to_date = date1

		pr.get_unreconciled_entries()
		# assert only si and pe are fetched
		self.assertEqual(len(pr.get("to_receive")), 2)
		self.assertEqual(len(pr.get("to_pay")), 2)

	def test_filter_posting_date_case2(self):
		"""
		Posting date should not affect outstanding amount calculation
		"""

		from_date = add_days(nowdate(), -30)
		to_date = nowdate()
		self.create_payment_entry(amount=25, posting_date=from_date).submit()
		self.create_sales_invoice(rate=25, qty=1, posting_date=to_date)

		pr = self.create_payment_reconciliation()
		pr.from_date = from_date
		pr.to_date = to_date
		pr.get_unreconciled_entries()

		self.assertEqual(len(pr.to_receive), 1)
		self.assertEqual(len(pr.to_pay), 1)

		pr.allocate_entries()
		pr.reconcile()

		pr.get_unreconciled_entries()

		self.assertEqual(len(pr.to_receive), 0)
		self.assertEqual(len(pr.to_pay), 0)

		pr.from_date = to_date
		pr.to_date = to_date

		pr.get_unreconciled_entries()

		self.assertEqual(len(pr.to_receive), 0)

	def test_filter_fetch_limit(self):
		transaction_date = nowdate()
		rate = 100
		for _i in range(5):
			self.create_sales_invoice(qty=1, rate=rate, posting_date=transaction_date)
			self.create_payment_entry(amount=rate, posting_date=transaction_date).save().submit()

		pr = self.create_payment_reconciliation()
		pr.from_date = pr.to_date = transaction_date
		pr.fetch_limit = 2
		pr.get_unreconciled_entries()

		self.assertEqual(len(pr.get("to_receive")), 2)
		self.assertEqual(len(pr.get("to_pay")), 2)

	def test_payment_against_invoice(self):
		si = self.create_sales_invoice(qty=1, rate=200)
		pe = self.create_payment_entry(amount=55).save().submit()
		# second payment entry
		self.create_payment_entry(amount=35).save().submit()

		pr = self.create_payment_reconciliation()

		# reconcile multiple payments against invoice
		pr.get_unreconciled_entries()
		pr.allocate_entries()

		# Difference amount should not be calculated for base currency accounts
		for row in pr.allocation:
			self.assertEqual(flt(row.get("difference_amount")), 0.0)

		pr.reconcile()

		si.reload()
		self.assertEqual(si.status, "Partly Paid")
		# check PR tool output post reconciliation
		self.assertEqual(len(pr.get("to_receive")), 1)
		self.assertEqual(pr.get("to_receive")[0].get("outstanding_amount"), 110)
		self.assertEqual(pr.get("to_pay"), [])

		# cancel one PE
		pe.reload()
		pe.cancel()
		pr.get_unreconciled_entries()
		# check PR tool output
		self.assertEqual(len(pr.get("to_receive")), 1)
		self.assertEqual(len(pr.get("to_pay")), 0)
		self.assertEqual(pr.get("to_receive")[0].get("outstanding_amount"), 165)

	def test_payment_against_journal(self):
		transaction_date = nowdate()

		sales = "Sales - _PR"
		amount = 921
		# debit debtors account to record an invoice
		je = self.create_journal_entry(self.debit_to, sales, amount, transaction_date)
		je.accounts[0].party_type = "Customer"
		je.accounts[0].party = self.customer
		je.save()
		je.submit()

		self.create_payment_entry(amount=amount, posting_date=transaction_date).save().submit()

		pr = self.create_payment_reconciliation()
		pr.min_amount = pr.max_amount = amount
		pr.from_date = pr.to_date = transaction_date
		pr.from_date = pr.to_date = transaction_date

		pr.get_unreconciled_entries()
		pr.allocate_entries()

		# Difference amount should not be calculated for base currency accounts
		for row in pr.allocation:
			self.assertEqual(flt(row.get("difference_amount")), 0.0)

		pr.reconcile()

		# check PR tool output
		self.assertEqual(len(pr.get("to_receive")), 0)
		self.assertEqual(len(pr.get("to_pay")), 0)

	def test_payment_against_foreign_currency_journal(self):
		transaction_date = nowdate()

		self.supplier = "_Test Supplier USD"
		self.supplier2 = make_supplier("_Test Supplier2 USD", "USD")
		amount = 100
		exc_rate1 = 80
		exc_rate2 = 83

		je = frappe.new_doc("Journal Entry")
		je.posting_date = transaction_date
		je.company = self.company
		je.user_remark = "test"
		je.multi_currency = 1
		je.set(
			"accounts",
			[
				{
					"account": self.creditors_usd,
					"party_type": "Supplier",
					"party": self.supplier,
					"exchange_rate": exc_rate1,
					"cost_center": self.cost_center,
					"credit": amount * exc_rate1,
					"credit_in_account_currency": amount,
				},
				{
					"account": self.creditors_usd,
					"party_type": "Supplier",
					"party": self.supplier2,
					"exchange_rate": exc_rate2,
					"cost_center": self.cost_center,
					"credit": amount * exc_rate2,
					"credit_in_account_currency": amount,
				},
				{
					"account": self.expense_account,
					"cost_center": self.cost_center,
					"debit": (amount * exc_rate1) + (amount * exc_rate2),
					"debit_in_account_currency": (amount * exc_rate1) + (amount * exc_rate2),
				},
			],
		)
		je.save().submit()

		pe = self.create_payment_entry(amount=amount, posting_date=transaction_date)
		pe.payment_type = "Pay"
		pe.party_type = "Supplier"
		pe.party = self.supplier
		pe.paid_to = self.creditors_usd
		pe.paid_from = self.cash
		pe.paid_amount = 8000
		pe.received_amount = 100
		pe.target_exchange_rate = exc_rate1
		pe.paid_to_account_currency = "USD"
		pe.save().submit()

		pr = self.create_payment_reconciliation(party_is_customer=False)
		pr.receivable_payable_account = self.creditors_usd
		pr.min_amount = pr.max_amount = amount
		pr.from_date = pr.to_date = transaction_date
		pr.from_date = pr.to_date = transaction_date

		pr.get_unreconciled_entries()
		pr.allocate_entries()

		# There should no difference_amount as the Journal and Payment have same exchange rate -  'exc_rate1'
		for row in pr.allocation:
			self.assertEqual(flt(row.get("difference_amount")), 0.0)

		pr.reconcile()

		# check PR tool output
		self.assertEqual(len(pr.get("to_receive")), 0)
		self.assertEqual(len(pr.get("to_pay")), 0)

		journals = frappe.db.get_all(
			"Journal Entry Account",
			filters={"reference_type": je.doctype, "reference_name": je.name, "docstatus": 1},
			fields=["parent"],
		)
		self.assertEqual([], journals)

	def test_journal_against_invoice(self):
		transaction_date = nowdate()
		amount = 100
		si = self.create_sales_invoice(qty=1, rate=amount, posting_date=transaction_date)

		# credit debtors account to record a payment
		je = self.create_journal_entry(self.bank, self.debit_to, amount, transaction_date)
		je.accounts[1].party_type = "Customer"
		je.accounts[1].party = self.customer
		je.save()
		je.submit()

		pr = self.create_payment_reconciliation()

		pr.get_unreconciled_entries()
		pr.allocate_entries()

		# Difference amount should not be calculated for base currency accounts
		for row in pr.allocation:
			self.assertEqual(flt(row.get("difference_amount")), 0.0)

		pr.reconcile()

		# assert outstanding
		si.reload()
		self.assertEqual(si.status, "Paid")
		self.assertEqual(si.outstanding_amount, 0)

		# check PR tool output
		self.assertEqual(len(pr.get("to_receive")), 0)
		self.assertEqual(len(pr.get("to_pay")), 0)

	def test_negative_debit_or_credit_journal_against_invoice(self):
		transaction_date = nowdate()
		amount = 100
		si = self.create_sales_invoice(qty=1, rate=amount, posting_date=transaction_date)

		# credit debtors account to record a payment
		je = self.create_journal_entry(self.bank, self.debit_to, amount, transaction_date)
		je.accounts[1].party_type = "Customer"
		je.accounts[1].party = self.customer
		je.accounts[1].credit_in_account_currency = 0
		je.accounts[1].debit_in_account_currency = -1 * amount
		je.save()
		je.submit()

		pr = self.create_payment_reconciliation()

		pr.get_unreconciled_entries()
		pr.allocate_entries()

		# Difference amount should not be calculated for base currency accounts
		for row in pr.allocation:
			self.assertEqual(flt(row.get("difference_amount")), 0.0)

		pr.reconcile()

		# assert outstanding
		si.reload()
		self.assertEqual(si.status, "Paid")
		self.assertEqual(si.outstanding_amount, 0)

		# check PR tool output
		self.assertEqual(len(pr.get("to_receive")), 0)
		self.assertEqual(len(pr.get("to_pay")), 0)

	def test_journal_against_journal(self):
		transaction_date = nowdate()
		sales = "Sales - _PR"
		amount = 100

		# debit debtors account to simulate a invoice
		je1 = self.create_journal_entry(self.debit_to, sales, amount, transaction_date)
		je1.accounts[0].party_type = "Customer"
		je1.accounts[0].party = self.customer
		je1.save()
		je1.submit()

		# credit debtors account to simulate a payment
		je2 = self.create_journal_entry(self.bank, self.debit_to, amount, transaction_date)
		je2.accounts[1].party_type = "Customer"
		je2.accounts[1].party = self.customer
		je2.save()
		je2.submit()

		pr = self.create_payment_reconciliation()

		pr.get_unreconciled_entries()
		pr.allocate_entries()

		# Difference amount should not be calculated for base currency accounts
		for row in pr.allocation:
			self.assertEqual(flt(row.get("difference_amount")), 0.0)

		pr.reconcile()

		self.assertEqual(pr.get("to_receive"), [])
		self.assertEqual(pr.get("to_pay"), [])

	def test_partial_journal_against_journal(self):
		"""Partial JE↔JE reconcile: the unsettled remainder stays visible per PLE
		netting (`_query_je_outstanding` Phase 2.5)."""
		transaction_date = nowdate()
		sales = "Sales - _PR"

		# invoice-like: Dr debtors 100
		je1 = self.create_journal_entry(self.debit_to, sales, 100, transaction_date)
		je1.accounts[0].party_type = "Customer"
		je1.accounts[0].party = self.customer
		je1.save().submit()

		# payment-like: Cr debtors 60
		je2 = self.create_journal_entry(self.bank, self.debit_to, 60, transaction_date)
		je2.accounts[1].party_type = "Customer"
		je2.accounts[1].party = self.customer
		je2.save().submit()

		pr = self.create_payment_reconciliation()
		pr.get_unreconciled_entries()
		self.assertEqual(len(pr.to_receive), 1)
		self.assertEqual(len(pr.to_pay), 1)

		pr.allocate_entries()
		pr.reconcile()  # re-fetches via PLE at the end

		# JE2 (60) fully consumed; JE1 keeps residual 40 netted from PLE.
		je1_rows = [r for r in pr.to_receive if r.voucher_no == je1.name]
		self.assertEqual(len(je1_rows), 1)
		self.assertEqual(flt(je1_rows[0].outstanding_amount), 40)
		self.assertEqual([r for r in pr.to_pay if r.voucher_no == je2.name], [])

	def test_negative_amount_journal_against_journal(self):
		"""A payment booked as a NEGATIVE debit on the party account (instead of a
		credit) reconciles against an invoice-like JE the same as a normal credit."""
		transaction_date = nowdate()
		sales = "Sales - _PR"

		# invoice-like: Dr debtors 100
		je1 = self.create_journal_entry(self.debit_to, sales, 100, transaction_date)
		je1.accounts[0].party_type = "Customer"
		je1.accounts[0].party = self.customer
		je1.save().submit()

		# payment-like booked as a negative debit on debtors (≡ credit 100)
		je2 = self.create_journal_entry(self.bank, self.debit_to, 100, transaction_date)
		je2.accounts[1].party_type = "Customer"
		je2.accounts[1].party = self.customer
		je2.accounts[1].credit_in_account_currency = 0
		je2.accounts[1].debit_in_account_currency = -100
		je2.save().submit()

		pr = self.create_payment_reconciliation()
		pr.get_unreconciled_entries()
		self.assertEqual(len(pr.to_receive), 1)
		self.assertEqual(len(pr.to_pay), 1)

		pr.allocate_entries()
		pr.reconcile()

		self.assertEqual(pr.get("to_receive"), [])
		self.assertEqual(pr.get("to_pay"), [])

	def test_journal_with_positive_and_negative_self_reconcile(self):
		"""One JE carrying both an invoice-like (+ve) and a payment-like (-ve) row
		on the same party account — the two rows reconcile against each other."""
		transaction_date = nowdate()
		je = frappe.new_doc("Journal Entry")
		je.posting_date = transaction_date
		je.company = self.company
		je.user_remark = "test"
		je.set(
			"accounts",
			[
				{  # invoice-like: Dr debtors 100
					"account": self.debit_to,
					"cost_center": self.cost_center,
					"party_type": "Customer",
					"party": self.customer,
					"debit_in_account_currency": 100,
					"credit_in_account_currency": 0,
				},
				{  # payment-like: Cr debtors 60
					"account": self.debit_to,
					"cost_center": self.cost_center,
					"party_type": "Customer",
					"party": self.customer,
					"debit_in_account_currency": 0,
					"credit_in_account_currency": 60,
				},
				{  # balancing leg
					"account": self.bank,
					"cost_center": self.cost_center,
					"debit_in_account_currency": 0,
					"credit_in_account_currency": 40,
				},
			],
		)
		je.save().submit()

		pr = self.create_payment_reconciliation()
		pr.get_unreconciled_entries()

		# Same JE → two party rows: +100 on to_receive, 60 on to_pay.
		recv = [r for r in pr.to_receive if r.voucher_no == je.name]
		pay = [r for r in pr.to_pay if r.voucher_no == je.name]
		self.assertEqual(len(recv), 1)
		self.assertEqual(len(pay), 1)
		self.assertEqual(flt(recv[0].outstanding_amount), 100)
		self.assertEqual(flt(pay[0].outstanding_amount), 60)

		pr.allocate_entries()
		pr.reconcile()

		# 60 of the +100 settled against the -60 row; residual 40 remains.
		recv2 = [r for r in pr.to_receive if r.voucher_no == je.name]
		self.assertEqual(len(recv2), 1)
		self.assertEqual(flt(recv2[0].outstanding_amount), 40)
		self.assertEqual([r for r in pr.to_pay if r.voucher_no == je.name], [])

	def test_cr_note_against_invoice(self):
		transaction_date = nowdate()
		amount = 100

		si = self.create_sales_invoice(qty=1, rate=amount, posting_date=transaction_date)

		cr_note = self.create_sales_invoice(
			qty=-1, rate=amount, posting_date=transaction_date, do_not_save=True, do_not_submit=True
		)
		cr_note.is_return = 1
		cr_note = cr_note.save().submit()

		pr = self.create_payment_reconciliation()

		pr.get_unreconciled_entries()
		pr.allocate_entries()

		# Cr Note and Invoice are of the same currency. There shouldn't any difference amount.
		for row in pr.allocation:
			self.assertEqual(flt(row.get("difference_amount")), 0.0)

		pr.reconcile()

		pr.get_unreconciled_entries()
		# check reconciliation tool output
		# reconciled invoice and credit note shouldn't show up in selection
		self.assertEqual(pr.get("to_receive"), [])
		self.assertEqual(pr.get("to_pay"), [])

		# assert outstanding
		si.reload()
		self.assertEqual(si.status, "Paid")
		self.assertEqual(si.outstanding_amount, 0)

	def test_invoice_status_after_cr_note_cancellation(self):
		# This test case is made after the 'always standalone Credit/Debit notes' feature is introduced
		transaction_date = nowdate()
		amount = 100

		si = self.create_sales_invoice(qty=1, rate=amount, posting_date=transaction_date)

		cr_note = self.create_sales_invoice(
			qty=-1, rate=amount, posting_date=transaction_date, do_not_save=True, do_not_submit=True
		)
		cr_note.is_return = 1
		cr_note.return_against = si.name
		cr_note = cr_note.save().submit()

		pr = self.create_payment_reconciliation()

		pr.get_unreconciled_entries()
		pr.allocate_entries()
		pr.reconcile()

		pr.get_unreconciled_entries()
		self.assertEqual(pr.get("to_receive"), [])
		self.assertEqual(pr.get("to_pay"), [])

		# A system 'Reconciliation Journal' bridge settled the pair — it carries the
		# invoice/note links on its JEA rows (not the JE-level reference fields).
		def bridge_for(reference_name):
			parents = frappe.db.get_all(
				"Journal Entry Account",
				filters={"reference_name": reference_name, "docstatus": 1},
				pluck="parent",
			)
			if not parents:
				return []
			return frappe.db.get_all(
				"Journal Entry",
				filters={
					"name": ["in", parents],
					"is_system_generated": 1,
					"docstatus": 1,
					"voucher_type": "Reconciliation Journal",
				},
				pluck="name",
			)

		self.assertEqual(len(set(bridge_for(si.name))), 1)

		# assert status and outstanding
		si.reload()
		self.assertEqual(si.status, "Credit Note Issued")
		self.assertEqual(si.outstanding_amount, 0)

		cr_note.reload()
		cr_note.cancel()
		# Cancelling the note auto-unwinds the bridge JE.
		self.assertEqual(len(set(bridge_for(si.name))), 0)
		# assert status and outstanding
		si.reload()
		self.assertEqual(si.status, "Unpaid")
		self.assertEqual(si.outstanding_amount, 100)

	def test_cr_note_partial_against_invoice(self):
		transaction_date = nowdate()
		amount = 100
		allocated_amount = 80

		si = self.create_sales_invoice(qty=1, rate=amount, posting_date=transaction_date)

		cr_note = self.create_sales_invoice(
			qty=-1, rate=amount, posting_date=transaction_date, do_not_save=True, do_not_submit=True
		)
		cr_note.is_return = 1
		cr_note = cr_note.save().submit()

		pr = self.create_payment_reconciliation()

		pr.get_unreconciled_entries()
		pr.allocate_entries()
		pr.allocation[0].allocated_amount = allocated_amount

		# Cr Note and Invoice are of the same currency. There shouldn't any difference amount.
		for row in pr.allocation:
			self.assertEqual(flt(row.get("difference_amount")), 0.0)

		pr.reconcile()

		# assert outstanding
		si.reload()
		self.assertEqual(si.status, "Partly Paid")
		self.assertEqual(si.outstanding_amount, 20)

		pr.get_unreconciled_entries()
		# check reconciliation tool output
		self.assertEqual(len(pr.get("to_receive")), 1)
		self.assertEqual(len(pr.get("to_pay")), 1)
		self.assertEqual(pr.get("to_receive")[0].outstanding_amount, 20)
		self.assertEqual(pr.get("to_pay")[0].amount, 20)

	def test_pr_output_foreign_currency_and_amount(self):
		# test for currency and amount invoices and payments
		transaction_date = nowdate()
		# In EUR
		amount = 100
		exchange_rate = 80

		si = self.create_sales_invoice(
			qty=1, rate=amount, posting_date=transaction_date, do_not_save=True, do_not_submit=True
		)
		si.customer = self.customer3
		si.currency = "EUR"
		si.conversion_rate = exchange_rate
		si.debit_to = self.debtors_eur
		si = si.save().submit()

		cr_note = self.create_sales_invoice(
			qty=-1, rate=amount, posting_date=transaction_date, do_not_save=True, do_not_submit=True
		)
		cr_note.customer = self.customer3
		cr_note.is_return = 1
		cr_note.currency = "EUR"
		cr_note.conversion_rate = exchange_rate
		cr_note.debit_to = self.debtors_eur
		cr_note = cr_note.save().submit()

		pr = self.create_payment_reconciliation()
		pr.party = self.customer3
		pr.receivable_payable_account = self.debtors_eur
		pr.get_unreconciled_entries()

		self.assertEqual(len(pr.to_receive), 1)
		self.assertEqual(len(pr.to_pay), 1)

		self.assertEqual(pr.to_receive[0].amount, amount)
		self.assertEqual(pr.to_receive[0].currency, "EUR")
		self.assertEqual(pr.to_pay[0].amount, amount)
		self.assertEqual(pr.to_pay[0].currency, "EUR")

		cr_note.cancel()

		pay = self.create_payment_entry(amount=amount, posting_date=transaction_date, customer=self.customer3)
		pay.paid_from = self.debtors_eur
		pay.paid_from_account_currency = "EUR"
		pay.source_exchange_rate = exchange_rate
		pay.received_amount = exchange_rate * amount
		pay = pay.save().submit()

		pr.get_unreconciled_entries()
		self.assertEqual(len(pr.to_receive), 1)
		self.assertEqual(len(pr.to_pay), 1)
		self.assertEqual(pr.to_pay[0].amount, amount)
		self.assertEqual(pr.to_pay[0].currency, "EUR")

	def test_difference_amount_via_journal_entry(self):
		# Make Sale Invoice
		si = self.create_sales_invoice(
			qty=1, rate=100, posting_date=nowdate(), do_not_save=True, do_not_submit=True
		)
		si.customer = self.customer4
		si.currency = "EUR"
		si.conversion_rate = 85
		si.debit_to = self.debtors_eur
		si.save().submit()

		# Make payment using Journal Entry
		je1 = self.create_journal_entry("HDFC - _PR", self.debtors_eur, 100, nowdate())
		je1.multi_currency = 1
		je1.accounts[0].exchange_rate = 1
		je1.accounts[0].credit_in_account_currency = 0
		je1.accounts[0].credit = 0
		je1.accounts[0].debit_in_account_currency = 8000
		je1.accounts[0].debit = 8000
		je1.accounts[1].party_type = "Customer"
		je1.accounts[1].party = self.customer4
		je1.accounts[1].exchange_rate = 80
		je1.accounts[1].credit_in_account_currency = 100
		je1.accounts[1].credit = 8000
		je1.accounts[1].debit_in_account_currency = 0
		je1.accounts[1].debit = 0
		je1.save()
		je1.submit()

		je2 = self.create_journal_entry("HDFC - _PR", self.debtors_eur, 200, nowdate())
		je2.multi_currency = 1
		je2.accounts[0].exchange_rate = 1
		je2.accounts[0].credit_in_account_currency = 0
		je2.accounts[0].credit = 0
		je2.accounts[0].debit_in_account_currency = 16000
		je2.accounts[0].debit = 16000
		je2.accounts[1].party_type = "Customer"
		je2.accounts[1].party = self.customer4
		je2.accounts[1].exchange_rate = 80
		je2.accounts[1].credit_in_account_currency = 200
		je1.accounts[1].credit = 16000
		je1.accounts[1].debit_in_account_currency = 0
		je1.accounts[1].debit = 0
		je2.save()
		je2.submit()

		pr = self.create_payment_reconciliation()
		pr.party = self.customer4
		pr.receivable_payable_account = self.debtors_eur
		pr.get_unreconciled_entries()

		self.assertEqual(len(pr.to_receive), 1)
		self.assertEqual(len(pr.to_pay), 2)

		# Test exact payment allocation
		to_receive_subset = [x.as_dict() for x in pr.to_receive]
		to_pay_subset = [pr.to_pay[0].as_dict()]
		pr.allocate_entries(to_receive=to_receive_subset, to_pay=to_pay_subset)

		self.assertEqual(pr.allocation[0].allocated_amount, 100)
		self.assertEqual(pr.allocation[0].difference_amount, -500)

		# Test partial payment allocation (with excess payment entry)
		pr.set("allocation", [])
		pr.get_unreconciled_entries()
		to_receive_subset = [x.as_dict() for x in pr.to_receive]
		to_pay_subset = [pr.to_pay[1].as_dict()]
		pr.allocate_entries(to_receive=to_receive_subset, to_pay=to_pay_subset)
		pr.allocation[0].difference_account = "Exchange Gain/Loss - _PR"

		self.assertEqual(pr.allocation[0].allocated_amount, 100)
		self.assertEqual(pr.allocation[0].difference_amount, -500)

		# Check if difference journal entry gets generated for difference amount after reconciliation
		pr.reconcile()
		total_credit_amount = frappe.db.get_all(
			"Journal Entry Account",
			{"account": self.debtors_eur, "docstatus": 1, "reference_name": si.name},
			[{"SUM": "credit", "as": "amount"}],
			group_by="reference_name",
		)[0].amount

		# total credit includes the exchange gain/loss amount
		self.assertEqual(flt(total_credit_amount, 2), 8500)

		jea_parent = frappe.db.get_all(
			"Journal Entry Account",
			filters={"account": self.debtors_eur, "docstatus": 1, "reference_name": si.name, "credit": 500},
			fields=["parent"],
		)[0]
		self.assertEqual(
			frappe.db.get_value("Journal Entry", jea_parent.parent, "voucher_type"), "Exchange Gain Or Loss"
		)

	def test_difference_amount_via_negative_debit_or_credit_journal_entry(self):
		# Make Sale Invoice
		si = self.create_sales_invoice(
			qty=1, rate=100, posting_date=nowdate(), do_not_save=True, do_not_submit=True
		)
		si.customer = self.customer4
		si.currency = "EUR"
		si.conversion_rate = 85
		si.debit_to = self.debtors_eur
		si.save().submit()

		# Make payment using Journal Entry
		je1 = self.create_journal_entry("HDFC - _PR", self.debtors_eur, 100, nowdate())
		je1.multi_currency = 1
		je1.accounts[0].exchange_rate = 1
		je1.accounts[0].credit_in_account_currency = -8000
		je1.accounts[0].credit = -8000
		je1.accounts[0].debit_in_account_currency = 0
		je1.accounts[0].debit = 0
		je1.accounts[1].party_type = "Customer"
		je1.accounts[1].party = self.customer4
		je1.accounts[1].exchange_rate = 80
		je1.accounts[1].credit_in_account_currency = 100
		je1.accounts[1].credit = 8000
		je1.accounts[1].debit_in_account_currency = 0
		je1.accounts[1].debit = 0
		je1.save()
		je1.submit()

		je2 = self.create_journal_entry("HDFC - _PR", self.debtors_eur, 200, nowdate())
		je2.multi_currency = 1
		je2.accounts[0].exchange_rate = 1
		je2.accounts[0].credit_in_account_currency = -16000
		je2.accounts[0].credit = -16000
		je2.accounts[0].debit_in_account_currency = 0
		je2.accounts[0].debit = 0
		je2.accounts[1].party_type = "Customer"
		je2.accounts[1].party = self.customer4
		je2.accounts[1].exchange_rate = 80
		je2.accounts[1].credit_in_account_currency = 200
		je1.accounts[1].credit = 16000
		je1.accounts[1].debit_in_account_currency = 0
		je1.accounts[1].debit = 0
		je2.save()
		je2.submit()

		pr = self.create_payment_reconciliation()
		pr.party = self.customer4
		pr.receivable_payable_account = self.debtors_eur
		pr.get_unreconciled_entries()

		self.assertEqual(len(pr.to_receive), 1)
		self.assertEqual(len(pr.to_pay), 2)

		# Test exact payment allocation
		to_receive_subset = [x.as_dict() for x in pr.to_receive]
		to_pay_subset = [pr.to_pay[0].as_dict()]
		pr.allocate_entries(to_receive=to_receive_subset, to_pay=to_pay_subset)

		self.assertEqual(pr.allocation[0].allocated_amount, 100)
		self.assertEqual(pr.allocation[0].difference_amount, -500)

		# Test partial payment allocation (with excess payment entry)
		pr.set("allocation", [])
		pr.get_unreconciled_entries()
		to_receive_subset = [x.as_dict() for x in pr.to_receive]
		to_pay_subset = [pr.to_pay[1].as_dict()]
		pr.allocate_entries(to_receive=to_receive_subset, to_pay=to_pay_subset)
		pr.allocation[0].difference_account = "Exchange Gain/Loss - _PR"

		self.assertEqual(pr.allocation[0].allocated_amount, 100)
		self.assertEqual(pr.allocation[0].difference_amount, -500)

		# Check if difference journal entry gets generated for difference amount after reconciliation
		pr.reconcile()
		total_credit_amount = frappe.db.get_all(
			"Journal Entry Account",
			{"account": self.debtors_eur, "docstatus": 1, "reference_name": si.name},
			[{"SUM": "credit", "as": "amount"}],
			group_by="reference_name",
		)[0].amount

		# total credit includes the exchange gain/loss amount
		self.assertEqual(flt(total_credit_amount, 2), 8500)

		jea_parent = frappe.db.get_all(
			"Journal Entry Account",
			filters={"account": self.debtors_eur, "docstatus": 1, "reference_name": si.name, "credit": 500},
			fields=["parent"],
		)[0]
		self.assertEqual(
			frappe.db.get_value("Journal Entry", jea_parent.parent, "voucher_type"), "Exchange Gain Or Loss"
		)

	def test_difference_amount_via_payment_entry(self):
		# Make Sale Invoice
		si = self.create_sales_invoice(
			qty=1, rate=100, posting_date=nowdate(), do_not_save=True, do_not_submit=True
		)
		si.customer = self.customer5
		si.currency = "EUR"
		si.conversion_rate = 85
		si.debit_to = self.debtors_eur
		si.save().submit()

		# Make payment using Payment Entry
		pe1 = create_payment_entry(
			company=self.company,
			payment_type="Receive",
			party_type="Customer",
			party=self.customer5,
			paid_from=self.debtors_eur,
			paid_to=self.bank,
			paid_amount=100,
		)

		pe1.source_exchange_rate = 80
		pe1.received_amount = 8000
		pe1.save()
		pe1.submit()

		pe2 = create_payment_entry(
			company=self.company,
			payment_type="Receive",
			party_type="Customer",
			party=self.customer5,
			paid_from=self.debtors_eur,
			paid_to=self.bank,
			paid_amount=200,
		)

		pe2.source_exchange_rate = 80
		pe2.received_amount = 16000
		pe2.save()
		pe2.submit()

		pr = self.create_payment_reconciliation()
		pr.party = self.customer5
		pr.receivable_payable_account = self.debtors_eur
		pr.get_unreconciled_entries()

		self.assertEqual(len(pr.to_receive), 1)
		self.assertEqual(len(pr.to_pay), 2)

		to_receive_subset = [x.as_dict() for x in pr.to_receive]
		to_pay_subset = [pr.to_pay[0].as_dict()]
		pr.allocate_entries(to_receive=to_receive_subset, to_pay=to_pay_subset)

		self.assertEqual(pr.allocation[0].allocated_amount, 100)
		self.assertEqual(pr.allocation[0].difference_amount, -500)

		pr.set("allocation", [])
		pr.get_unreconciled_entries()
		to_receive_subset = [x.as_dict() for x in pr.to_receive]
		to_pay_subset = [pr.to_pay[1].as_dict()]
		pr.allocate_entries(to_receive=to_receive_subset, to_pay=to_pay_subset)

		self.assertEqual(pr.allocation[0].allocated_amount, 100)
		self.assertEqual(pr.allocation[0].difference_amount, -500)

	def test_differing_cost_center_on_invoice_and_payment(self):
		"""
		Cost Center filter should not affect outstanding amount calculation
		"""

		si = self.create_sales_invoice(qty=1, rate=100, do_not_submit=True)
		si.cost_center = self.main_cc.name
		si.submit()
		pr = get_payment_entry(si.doctype, si.name)
		pr.cost_center = self.sub_cc.name
		pr = pr.save().submit()

		pr = self.create_payment_reconciliation()
		pr.cost_center = self.main_cc.name

		pr.get_unreconciled_entries()

		# check PR tool output
		self.assertEqual(len(pr.get("to_receive")), 0)
		self.assertEqual(len(pr.get("to_pay")), 0)

	def test_cost_center_filter_on_vouchers(self):
		"""
		Test Cost Center filter is applied on Invoices, Payment Entries and Journals
		"""
		transaction_date = nowdate()
		rate = 100

		# 'Main - PR' Cost Center
		si1 = self.create_sales_invoice(qty=1, rate=rate, posting_date=transaction_date, do_not_submit=True)
		si1.cost_center = self.main_cc.name
		si1.submit()

		pe1 = self.create_payment_entry(posting_date=transaction_date, amount=rate)
		pe1.cost_center = self.main_cc.name
		pe1 = pe1.save().submit()

		je1 = self.create_journal_entry(self.bank, self.debit_to, 100, transaction_date)
		je1.accounts[0].cost_center = self.main_cc.name
		je1.accounts[1].cost_center = self.main_cc.name
		je1.accounts[1].party_type = "Customer"
		je1.accounts[1].party = self.customer
		je1 = je1.save().submit()

		# 'Sub - PR' Cost Center
		si2 = self.create_sales_invoice(qty=1, rate=rate, posting_date=transaction_date, do_not_submit=True)
		si2.cost_center = self.sub_cc.name
		si2.submit()

		pe2 = self.create_payment_entry(posting_date=transaction_date, amount=rate)
		pe2.cost_center = self.sub_cc.name
		pe2 = pe2.save().submit()

		je2 = self.create_journal_entry(self.bank, self.debit_to, 100, transaction_date)
		je2.accounts[0].cost_center = self.sub_cc.name
		je2.accounts[1].cost_center = self.sub_cc.name
		je2.accounts[1].party_type = "Customer"
		je2.accounts[1].party = self.customer
		je2 = je2.save().submit()

		pr = self.create_payment_reconciliation()
		pr.cost_center = self.main_cc.name

		pr.get_unreconciled_entries()

		# check PR tool output
		self.assertEqual(len(pr.get("to_receive")), 1)
		self.assertEqual(pr.get("to_receive")[0].get("voucher_no"), si1.name)
		self.assertEqual(len(pr.get("to_pay")), 2)
		payment_vouchers = [x.get("voucher_no") for x in pr.get("to_pay")]
		self.assertCountEqual(payment_vouchers, [pe1.name, je1.name])

		# Change cost center
		pr.cost_center = self.sub_cc.name

		pr.get_unreconciled_entries()

		# check PR tool output
		self.assertEqual(len(pr.get("to_receive")), 1)
		self.assertEqual(pr.get("to_receive")[0].get("voucher_no"), si2.name)
		self.assertEqual(len(pr.get("to_pay")), 2)
		payment_vouchers = [x.get("voucher_no") for x in pr.get("to_pay")]
		self.assertCountEqual(payment_vouchers, [je2.name, pe2.name])

	@ERPNextTestSuite.change_settings(
		"Accounts Settings",
		{
			"allow_multi_currency_invoices_against_single_party_account": 1,
		},
	)
	def test_no_difference_amount_for_base_currency_accounts(self):
		# Make Sale Invoice
		si = self.create_sales_invoice(
			qty=1, rate=1, posting_date=nowdate(), do_not_save=True, do_not_submit=True
		)
		si.customer = self.customer
		si.currency = "EUR"
		si.conversion_rate = 85
		si.debit_to = self.debit_to
		si.save().submit()

		# Make payment using Payment Entry
		pe1 = create_payment_entry(
			company=self.company,
			payment_type="Receive",
			party_type="Customer",
			party=self.customer,
			paid_from=self.debit_to,
			paid_to=self.bank,
			paid_amount=100,
		)

		pe1.save()
		pe1.submit()

		pr = self.create_payment_reconciliation()
		pr.party = self.customer
		pr.receivable_payable_account = self.debit_to
		pr.get_unreconciled_entries()

		self.assertEqual(len(pr.to_receive), 1)
		self.assertEqual(len(pr.to_pay), 1)

		to_receive_subset = [x.as_dict() for x in pr.to_receive]
		to_pay_subset = [pr.to_pay[0].as_dict()]
		pr.allocate_entries(to_receive=to_receive_subset, to_pay=to_pay_subset)

		self.assertEqual(pr.allocation[0].allocated_amount, 85)
		self.assertEqual(pr.allocation[0].difference_amount, 0)

		pr.reconcile()
		si.reload()
		self.assertEqual(si.outstanding_amount, 0)
		# No Exchange Gain/Loss journal should be generated
		exc_gain_loss_journals = frappe.db.get_all(
			"Journal Entry Account",
			filters={"reference_type": si.doctype, "reference_name": si.name, "docstatus": 1},
			fields=["parent"],
		)
		self.assertEqual(exc_gain_loss_journals, [])

	def test_reconciliation_purchase_invoice_against_return(self):
		self.supplier = "_Test Supplier USD"
		pi = self.create_purchase_invoice(qty=5, rate=50, do_not_submit=True)
		pi.supplier = self.supplier
		pi.currency = "USD"
		pi.conversion_rate = 50
		pi.credit_to = self.creditors_usd
		pi.save().submit()

		pi_return = frappe.get_doc(pi.as_dict())
		pi_return.name = None
		pi_return.docstatus = 0
		pi_return.is_return = 1
		pi_return.conversion_rate = 80
		pi_return.items[0].qty = -pi_return.items[0].qty
		pi_return.submit()

		pr = frappe.get_doc("Payment Reconciliation")
		pr.company = self.company
		pr.party_type = "Supplier"
		pr.party = self.supplier
		pr.receivable_payable_account = self.creditors_usd
		pr.from_date = pr.to_date = nowdate()
		pr.get_unreconciled_entries()

		to_pay_subset = []
		to_receive_subset = []
		for row in pr.to_pay:
			if row.voucher_no == pi.name:
				to_pay_subset.append(row.as_dict())
				break
		for row in pr.to_receive:
			if row.voucher_no == pi_return.name:
				to_receive_subset.append(row.as_dict())
				break

		pr.allocate_entries(to_receive=to_receive_subset, to_pay=to_pay_subset)

		# Should not raise frappe.exceptions.ValidationError: Total Debit must be equal to Total Credit.
		pr.reconcile()

	def test_reconciliation_from_purchase_order_to_multiple_invoices(self):
		"""
		Reconciling advance payment from PO/SO to multiple invoices should not cause overallocation
		"""

		self.supplier = "_Test Supplier"

		pi1 = self.create_purchase_invoice(qty=10, rate=100)
		pi2 = self.create_purchase_invoice(qty=10, rate=100)
		po = self.create_purchase_order(qty=20, rate=100)
		pay = get_payment_entry(po.doctype, po.name)
		# Overpay Puchase Order
		pay.paid_amount = 3000
		pay.save().submit()
		# assert total allocated and unallocated before reconciliation
		self.assertEqual(
			(
				pay.references[0].reference_doctype,
				pay.references[0].reference_name,
				pay.references[0].allocated_amount,
			),
			(po.doctype, po.name, 2000),
		)
		self.assertEqual(pay.total_allocated_amount, 2000)
		self.assertEqual(pay.unallocated_amount, 1000)
		self.assertEqual(pay.difference_amount, 0)

		pr = self.create_payment_reconciliation(party_is_customer=False)
		pr.get_unreconciled_entries()

		self.assertEqual(len(pr.to_pay), 2)
		self.assertGreaterEqual(len(pr.to_receive), 1)

		for x in pr.to_receive:
			self.assertEqual((x.voucher_type, x.voucher_no), (pay.doctype, pay.name))

		pr.allocate_entries()
		# partial allocation on pi1 and full allocate on pi2
		pr.allocation[0].allocated_amount = 100
		pr.reconcile()

		# assert references and total allocated and unallocated amount
		pay.reload()
		self.assertEqual(len(pay.references), 3)
		self.assertEqual(
			(
				pay.references[0].reference_doctype,
				pay.references[0].reference_name,
				pay.references[0].allocated_amount,
			),
			(po.doctype, po.name, 900),
		)
		self.assertEqual(
			(
				pay.references[1].reference_doctype,
				pay.references[1].reference_name,
				pay.references[1].allocated_amount,
			),
			(pi1.doctype, pi1.name, 100),
		)
		self.assertEqual(
			(
				pay.references[2].reference_doctype,
				pay.references[2].reference_name,
				pay.references[2].allocated_amount,
			),
			(pi2.doctype, pi2.name, 1000),
		)
		self.assertEqual(pay.total_allocated_amount, 2000)
		self.assertEqual(pay.unallocated_amount, 1000)
		self.assertEqual(pay.difference_amount, 0)

		pr.get_unreconciled_entries()
		self.assertEqual(len(pr.to_pay), 1)
		self.assertGreaterEqual(len(pr.to_receive), 1)

		pr.allocate_entries()
		pr.reconcile()

		# assert references and total allocated and unallocated amount
		pay.reload()
		self.assertEqual(len(pay.references), 3)
		# PO references should be removed now
		self.assertEqual(
			(
				pay.references[0].reference_doctype,
				pay.references[0].reference_name,
				pay.references[0].allocated_amount,
			),
			(pi1.doctype, pi1.name, 100),
		)
		self.assertEqual(
			(
				pay.references[1].reference_doctype,
				pay.references[1].reference_name,
				pay.references[1].allocated_amount,
			),
			(pi2.doctype, pi2.name, 1000),
		)
		self.assertEqual(
			(
				pay.references[2].reference_doctype,
				pay.references[2].reference_name,
				pay.references[2].allocated_amount,
			),
			(pi1.doctype, pi1.name, 900),
		)
		self.assertEqual(pay.total_allocated_amount, 2000)
		self.assertEqual(pay.unallocated_amount, 1000)
		self.assertEqual(pay.difference_amount, 0)

	def test_pe_overpaying_po_emits_reference_and_unallocated_rows(self):
		"""PR-E (`OpenBalanceFetcher._split_pe_by_references`): a PO of `x` paid by
		a PE of `x + y` must surface as two re-pointable slices — a bound row of
		`x` (voucher_row=PER.name) and a free row of `y` (no voucher_row).
		"""
		self.supplier = "_Test Supplier"

		x, y = 2000, 1000  # PO-allocated, overpaid surplus

		po = self.create_purchase_order(qty=20, rate=100)
		pay = get_payment_entry(po.doctype, po.name)
		pay.paid_amount = x + y
		pay.save().submit()

		self.assertEqual(pay.references[0].reference_doctype, po.doctype)
		self.assertEqual(pay.references[0].reference_name, po.name)
		self.assertEqual(pay.references[0].allocated_amount, x)
		self.assertEqual(pay.unallocated_amount, y)
		per_name = pay.references[0].name

		pr = self.create_payment_reconciliation(party_is_customer=False)
		pr.get_unreconciled_entries()

		# Supplier advance lands on to_receive.
		pe_rows = [r for r in pr.to_receive if r.voucher_no == pay.name]
		self.assertEqual(len(pe_rows), 2)

		ref_rows = [r for r in pe_rows if r.voucher_row]
		self_rows = [r for r in pe_rows if not r.voucher_row]
		self.assertEqual(len(ref_rows), 1)
		self.assertEqual(len(self_rows), 1)

		self.assertEqual(ref_rows[0].voucher_row, per_name)
		self.assertEqual(flt(ref_rows[0].outstanding_amount), x)
		self.assertEqual(flt(self_rows[0].outstanding_amount), y)
		self.assertEqual(ref_rows[0].account, self_rows[0].account)

		# Bound slice carries the source PO/SO; free slice is unbound (blank).
		self.assertEqual(ref_rows[0].reference_doctype, po.doctype)
		self.assertEqual(ref_rows[0].reference_name, po.name)
		self.assertFalse(self_rows[0].reference_name)

	def test_rounding_of_unallocated_amount(self):
		self.supplier = "_Test Supplier USD"
		pi = self.create_purchase_invoice(qty=1, rate=10, do_not_submit=True)
		pi.supplier = self.supplier
		pi.currency = "USD"
		pi.conversion_rate = 80
		pi.credit_to = self.creditors_usd
		pi.save().submit()

		pe = get_payment_entry(pi.doctype, pi.name)
		pe.target_exchange_rate = 78.726500000
		pe.received_amount = 26.75
		pe.paid_amount = 2105.93
		pe.references = []
		pe.save().submit()

		# unallocated_amount will have some rounding loss - 26.749950
		self.assertNotEqual(pe.unallocated_amount, 26.75)

		pr = frappe.get_doc("Payment Reconciliation")
		pr.company = self.company
		pr.party_type = "Supplier"
		pr.party = self.supplier
		pr.receivable_payable_account = self.creditors_usd
		pr.from_date = pr.to_date = nowdate()
		pr.get_unreconciled_entries()

		pr.allocate_entries()

		# Should not raise frappe.exceptions.ValidationError: Payment Entry has been modified after you pulled it. Please pull it again.
		pr.reconcile()

	def test_reverse_payment_against_payment_for_supplier(self):
		"""
		Reconcile a payment against a reverse payment, for a supplier.
		"""
		self.supplier = "_Test Supplier"
		amount = 4000

		pe = self.create_payment_entry(amount=amount)
		pe.party_type = "Supplier"
		pe.party = self.supplier
		pe.payment_type = "Pay"
		pe.paid_from = self.cash
		pe.paid_to = self.creditors
		pe.save().submit()

		reverse_pe = self.create_payment_entry(amount=amount)
		reverse_pe.party_type = "Supplier"
		reverse_pe.party = self.supplier
		reverse_pe.payment_type = "Receive"
		reverse_pe.paid_from = self.creditors
		reverse_pe.paid_to = self.cash
		reverse_pe.save().submit()

		pr = self.create_payment_reconciliation(party_is_customer=False)
		pr.get_unreconciled_entries()
		self.assertEqual(len(pr.to_receive), 1)
		self.assertEqual(len(pr.to_pay), 1)
		self.assertEqual(pr.to_receive[0].voucher_no, pe.name)
		self.assertEqual(pr.to_pay[0].voucher_no, reverse_pe.name)

		pr.allocate_entries()
		pr.reconcile()

		pe.reload()
		self.assertEqual(len(pe.references), 1)
		self.assertEqual(pe.references[0].exchange_rate, 1)
		self.assertEqual(pe.references[0].exchange_gain_loss, 0)
		self.assertEqual(pe.references[0].reference_name, reverse_pe.name)

		reverse_pe.reload()
		self.assertEqual(reverse_pe.references, [])

		journals = frappe.db.get_all(
			"Journal Entry",
			filters={
				"voucher_type": "Exchange Gain Or Loss",
				"reference_type": "Payment Entry",
				"reference_name": ("in", [pe.name, reverse_pe.name]),
			},
		)
		# There should be no Exchange Gain/Loss created
		self.assertEqual(journals, [])

	def test_advance_reverse_payment_against_payment_for_supplier(self):
		"""
		Reconcile an Advance payment against reverse payment, for a supplier.
		"""
		frappe.db.set_value(
			"Company",
			self.company,
			{
				"book_advance_payments_in_separate_party_account": 1,
				"default_advance_paid_account": self.advance_payable_account,
			},
		)

		self.supplier = "_Test Supplier"
		amount = 4000

		pe = self.create_payment_entry(amount=amount)
		pe.party_type = "Supplier"
		pe.party = self.supplier
		pe.payment_type = "Pay"
		pe.paid_from = self.cash
		pe.paid_to = self.advance_payable_account
		pe.save().submit()

		reverse_pe = self.create_payment_entry(amount=amount)
		reverse_pe.party_type = "Supplier"
		reverse_pe.party = self.supplier
		reverse_pe.payment_type = "Receive"
		reverse_pe.paid_from = self.advance_payable_account
		reverse_pe.paid_to = self.cash
		reverse_pe.save().submit()

		pr = self.create_payment_reconciliation(party_is_customer=False)
		pr.default_advance_account = self.advance_payable_account
		pr.get_unreconciled_entries()
		self.assertEqual(len(pr.to_receive), 1)
		self.assertEqual(len(pr.to_pay), 1)
		self.assertEqual(pr.to_receive[0].voucher_no, pe.name)
		self.assertEqual(pr.to_pay[0].voucher_no, reverse_pe.name)

		pr.allocate_entries()
		pr.reconcile()

		pe.reload()
		self.assertEqual(len(pe.references), 1)
		self.assertEqual(pe.references[0].exchange_rate, 1)
		self.assertEqual(pe.references[0].exchange_gain_loss, 0)
		self.assertEqual(pe.references[0].reference_name, reverse_pe.name)

		reverse_pe.reload()
		self.assertEqual(reverse_pe.references, [])

		journals = frappe.db.get_all(
			"Journal Entry",
			filters={
				"voucher_type": "Exchange Gain Or Loss",
				"reference_type": "Payment Entry",
				"reference_name": ("in", [pe.name, reverse_pe.name]),
			},
		)
		# There should be no Exchange Gain/Loss created
		self.assertEqual(journals, [])

		# Assert Ledger Entries
		gl_entries = frappe.db.get_all(
			"GL Entry",
			filters={"voucher_no": pe.name},
			fields=["account", "voucher_no", "against_voucher", "debit", "credit"],
			order_by="account, against_voucher, debit",
		)
		expected_gle = [
			{
				"account": self.advance_payable_account,
				"voucher_no": pe.name,
				"against_voucher": pe.name,
				"debit": 0.0,
				"credit": amount,
			},
			{
				"account": self.advance_payable_account,
				"voucher_no": pe.name,
				"against_voucher": pe.name,
				"debit": amount,
				"credit": 0.0,
			},
			{
				"account": self.advance_payable_account,
				"voucher_no": pe.name,
				"against_voucher": reverse_pe.name,
				"debit": amount,
				"credit": 0.0,
			},
			{
				"account": "Cash - _PR",
				"voucher_no": pe.name,
				"against_voucher": None,
				"debit": 0.0,
				"credit": amount,
			},
		]
		self.assertEqual(gl_entries, expected_gle)
		pl_entries = frappe.db.get_all(
			"Payment Ledger Entry",
			filters={"voucher_no": pe.name},
			fields=["account", "voucher_no", "against_voucher_no", "amount"],
			order_by="account, against_voucher_no, amount",
		)
		expected_ple = [
			{
				"account": self.advance_payable_account,
				"voucher_no": pe.name,
				"against_voucher_no": pe.name,
				"amount": -amount,
			},
			{
				"account": self.advance_payable_account,
				"voucher_no": pe.name,
				"against_voucher_no": pe.name,
				"amount": amount,
			},
			{
				"account": self.advance_payable_account,
				"voucher_no": pe.name,
				"against_voucher_no": reverse_pe.name,
				"amount": -amount,
			},
		]
		self.assertEqual(pl_entries, expected_ple)

		pr.get_unreconciled_entries()
		self.assertEqual(len(pr.to_receive), 0)
		self.assertEqual(len(pr.to_pay), 0)

	def test_advance_payment_reconciliation_date(self):
		frappe.db.set_value(
			"Company",
			self.company,
			{
				"book_advance_payments_in_separate_party_account": 1,
				"default_advance_paid_account": self.advance_payable_account,
				"reconciliation_takes_effect_on": "Advance Payment Date",
			},
		)

		self.supplier = "_Test Supplier"
		amount = 1500

		pe = self.create_payment_entry(amount=amount)
		pe.posting_date = add_days(nowdate(), -1)
		pe.party_type = "Supplier"
		pe.party = self.supplier
		pe.payment_type = "Pay"
		pe.paid_from = self.cash
		pe.paid_to = self.advance_payable_account
		pe.save().submit()

		pi = self.create_purchase_invoice(qty=10, rate=100)
		self.assertNotEqual(pe.posting_date, pi.posting_date)

		pr = self.create_payment_reconciliation(party_is_customer=False)
		pr.default_advance_account = self.advance_payable_account
		pr.from_date = pe.posting_date
		pr.get_unreconciled_entries()
		self.assertEqual(len(pr.to_receive), 1)
		self.assertEqual(len(pr.to_pay), 1)
		pr.allocate_entries()
		pr.reconcile()

		# Assert Ledger Entries
		gl_entries = frappe.db.get_all(
			"GL Entry",
			filters={"voucher_no": pe.name, "is_cancelled": 0, "posting_date": pe.posting_date},
		)
		self.assertEqual(len(gl_entries), 4)
		pl_entries = frappe.db.get_all(
			"Payment Ledger Entry",
			filters={"voucher_no": pe.name, "delinked": 0, "posting_date": pe.posting_date},
		)
		self.assertEqual(len(pl_entries), 3)

	def test_advance_payment_reconciliation_date_for_older_date(self):
		old_settings = frappe.db.get_value(
			"Company",
			self.company,
			[
				"reconciliation_takes_effect_on",
				"default_advance_paid_account",
				"book_advance_payments_in_separate_party_account",
			],
			as_dict=True,
		)
		frappe.db.set_value(
			"Company",
			self.company,
			{
				"book_advance_payments_in_separate_party_account": 1,
				"default_advance_paid_account": self.advance_payable_account,
				"reconciliation_takes_effect_on": "Oldest Of Invoice Or Advance",
			},
		)

		self.supplier = "_Test Supplier"

		pi1 = self.create_purchase_invoice(qty=10, rate=100)
		po = self.create_purchase_order(qty=10, rate=100)

		pay = get_payment_entry(po.doctype, po.name)
		pay.paid_amount = 1000
		pay.save().submit()

		pr = frappe.new_doc("Payment Reconciliation")
		pr.company = self.company
		pr.party_type = "Supplier"
		pr.party = self.supplier
		pr.receivable_payable_account = get_party_account(pr.party_type, pr.party, pr.company)
		pr.default_advance_account = self.advance_payable_account
		pr.get_unreconciled_entries()
		pr.allocate_entries()
		pr.allocation[0].allocated_amount = 100
		pr.reconcile()

		pay.reload()
		self.assertEqual(getdate(pay.references[0].reconcile_effect_on), getdate(pi1.posting_date))

		# test setting of date if not available
		frappe.db.set_value("Payment Entry Reference", pay.references[1].name, "reconcile_effect_on", None)
		pay.reload()
		pay.cancel()

		pay.reload()
		pi1.reload()
		po.reload()

		self.assertEqual(getdate(pay.references[0].reconcile_effect_on), getdate(pi1.posting_date))
		pi1.cancel()
		po.cancel()

		frappe.db.set_value("Company", self.company, old_settings)

	def test_advance_payment_reconciliation_against_journal_for_customer(self):
		frappe.db.set_value(
			"Company",
			self.company,
			{
				"book_advance_payments_in_separate_party_account": 1,
				"default_advance_received_account": self.advance_receivable_account,
				"reconciliation_takes_effect_on": "Oldest Of Invoice Or Advance",
			},
		)
		amount = 200.0
		je = self.create_journal_entry(self.debit_to, self.bank, amount)
		je.accounts[0].cost_center = self.main_cc.name
		je.accounts[0].party_type = "Customer"
		je.accounts[0].party = self.customer
		je.accounts[1].cost_center = self.main_cc.name
		je = je.save().submit()

		pe = self.create_payment_entry(amount=amount).save().submit()

		pr = self.create_payment_reconciliation()
		pr.default_advance_account = self.advance_receivable_account
		pr.get_unreconciled_entries()
		self.assertEqual(len(pr.to_receive), 1)
		self.assertEqual(len(pr.to_pay), 1)
		pr.allocate_entries()
		pr.reconcile()

		# Assert Ledger Entries
		gl_entries = frappe.db.get_all(
			"GL Entry",
			filters={"voucher_no": pe.name, "is_cancelled": 0},
		)
		self.assertEqual(len(gl_entries), 4)
		pl_entries = frappe.db.get_all(
			"Payment Ledger Entry",
			filters={"voucher_no": pe.name, "delinked": 0},
		)
		self.assertEqual(len(pl_entries), 3)

		gl_entries = frappe.db.get_all(
			"GL Entry",
			filters={"voucher_no": pe.name, "is_cancelled": 0},
			fields=["account", "voucher_no", "against_voucher", "debit", "credit"],
			order_by="account, against_voucher, debit",
		)
		expected_gle = [
			{
				"account": self.advance_receivable_account,
				"voucher_no": pe.name,
				"against_voucher": pe.name,
				"debit": 0.0,
				"credit": amount,
			},
			{
				"account": self.advance_receivable_account,
				"voucher_no": pe.name,
				"against_voucher": pe.name,
				"debit": amount,
				"credit": 0.0,
			},
			{
				"account": self.debit_to,
				"voucher_no": pe.name,
				"against_voucher": je.name,
				"debit": 0.0,
				"credit": amount,
			},
			{
				"account": self.bank,
				"voucher_no": pe.name,
				"against_voucher": None,
				"debit": amount,
				"credit": 0.0,
			},
		]
		self.assertEqual(gl_entries, expected_gle)

		pl_entries = frappe.db.get_all(
			"Payment Ledger Entry",
			filters={"voucher_no": pe.name},
			fields=["account", "voucher_no", "against_voucher_no", "amount"],
			order_by="account, against_voucher_no, amount",
		)
		expected_ple = [
			{
				"account": self.advance_receivable_account,
				"voucher_no": pe.name,
				"against_voucher_no": pe.name,
				"amount": -amount,
			},
			{
				"account": self.advance_receivable_account,
				"voucher_no": pe.name,
				"against_voucher_no": pe.name,
				"amount": amount,
			},
			{
				"account": self.debit_to,
				"voucher_no": pe.name,
				"against_voucher_no": je.name,
				"amount": -amount,
			},
		]
		self.assertEqual(pl_entries, expected_ple)

	def test_advance_payment_reconciliation_against_journal_for_supplier(self):
		self.supplier = make_supplier("_Test Supplier")
		frappe.db.set_value(
			"Company",
			self.company,
			{
				"book_advance_payments_in_separate_party_account": 1,
				"default_advance_paid_account": self.advance_payable_account,
				"reconciliation_takes_effect_on": "Oldest Of Invoice Or Advance",
			},
		)
		amount = 200.0
		je = self.create_journal_entry(self.creditors, self.bank, -amount)
		je.accounts[0].cost_center = self.main_cc.name
		je.accounts[0].party_type = "Supplier"
		je.accounts[0].party = self.supplier
		je.accounts[1].cost_center = self.main_cc.name
		je = je.save().submit()

		pe = self.create_payment_entry(amount=amount)
		pe.payment_type = "Pay"
		pe.party_type = "Supplier"
		pe.paid_from = self.bank
		pe.paid_to = self.creditors
		pe.party = self.supplier
		pe.save().submit()

		pr = self.create_payment_reconciliation(party_is_customer=False)
		pr.default_advance_account = self.advance_payable_account
		pr.get_unreconciled_entries()
		self.assertEqual(len(pr.to_receive), 1)
		self.assertEqual(len(pr.to_pay), 1)
		pr.allocate_entries()
		pr.reconcile()

		# Assert Ledger Entries
		gl_entries = frappe.db.get_all(
			"GL Entry",
			filters={"voucher_no": pe.name, "is_cancelled": 0},
		)
		self.assertEqual(len(gl_entries), 4)
		pl_entries = frappe.db.get_all(
			"Payment Ledger Entry",
			filters={"voucher_no": pe.name, "delinked": 0},
		)
		self.assertEqual(len(pl_entries), 3)

		gl_entries = frappe.db.get_all(
			"GL Entry",
			filters={"voucher_no": pe.name, "is_cancelled": 0},
			fields=["account", "voucher_no", "against_voucher", "debit", "credit"],
			order_by="account, against_voucher, debit",
		)
		expected_gle = [
			{
				"account": self.advance_payable_account,
				"voucher_no": pe.name,
				"against_voucher": pe.name,
				"debit": 0.0,
				"credit": amount,
			},
			{
				"account": self.advance_payable_account,
				"voucher_no": pe.name,
				"against_voucher": pe.name,
				"debit": amount,
				"credit": 0.0,
			},
			{
				"account": self.creditors,
				"voucher_no": pe.name,
				"against_voucher": je.name,
				"debit": amount,
				"credit": 0.0,
			},
			{
				"account": self.bank,
				"voucher_no": pe.name,
				"against_voucher": None,
				"debit": 0.0,
				"credit": amount,
			},
		]
		self.assertEqual(gl_entries, expected_gle)

		pl_entries = frappe.db.get_all(
			"Payment Ledger Entry",
			filters={"voucher_no": pe.name},
			fields=["account", "voucher_no", "against_voucher_no", "amount"],
			order_by="account, against_voucher_no, amount",
		)
		expected_ple = [
			{
				"account": self.advance_payable_account,
				"voucher_no": pe.name,
				"against_voucher_no": pe.name,
				"amount": -amount,
			},
			{
				"account": self.advance_payable_account,
				"voucher_no": pe.name,
				"against_voucher_no": pe.name,
				"amount": amount,
			},
			{
				"account": self.creditors,
				"voucher_no": pe.name,
				"against_voucher_no": je.name,
				"amount": -amount,
			},
		]
		self.assertEqual(pl_entries, expected_ple)

	def test_cr_note_fetch_limit(self):
		transaction_date = nowdate()
		amount = 100

		for _ in range(6):
			self.create_sales_invoice(qty=1, rate=amount, posting_date=transaction_date)
			cr_note = self.create_sales_invoice(
				qty=-1, rate=amount, posting_date=transaction_date, do_not_save=True, do_not_submit=True
			)
			cr_note.is_return = 1
			cr_note = cr_note.save().submit()

		pr = self.create_payment_reconciliation()

		pr.get_unreconciled_entries()
		self.assertEqual(len(pr.to_receive), 6)
		self.assertEqual(len(pr.to_pay), 6)
		pr.allocate_entries()
		pr.reconcile()

		pr.get_unreconciled_entries()
		self.assertEqual(pr.get("to_receive"), [])
		self.assertEqual(pr.get("to_pay"), [])

		self.create_sales_invoice(qty=1, rate=amount, posting_date=transaction_date)
		cr_note = self.create_sales_invoice(
			qty=-1, rate=amount, posting_date=transaction_date, do_not_save=True, do_not_submit=True
		)
		cr_note.is_return = 1
		cr_note = cr_note.save().submit()

		# Limit should not prevent fetching the lone unallocated SI/CN pair.
		pr.fetch_limit = 5
		pr.get_unreconciled_entries()
		self.assertEqual(len(pr.to_receive), 1)
		self.assertEqual(len(pr.to_pay), 1)

	def test_reconciliation_on_closed_period_payment(self):
		# create backdated fiscal year
		first_fy_start_date = frappe.db.get_value(
			"Fiscal Year", {"disabled": 0}, [{"MIN": "year_start_date"}]
		)
		prev_fy_start_date = add_years(first_fy_start_date, -1)
		prev_fy_end_date = add_days(first_fy_start_date, -1)
		create_fiscal_year(
			company=self.company, year_start_date=prev_fy_start_date, year_end_date=prev_fy_end_date
		)

		# make journal entry for previous year
		je_1 = frappe.new_doc("Journal Entry")
		je_1.posting_date = add_days(prev_fy_start_date, 20)
		je_1.company = self.company
		je_1.user_remark = "test"
		je_1.set(
			"accounts",
			[
				{
					"account": self.debit_to,
					"cost_center": self.cost_center,
					"party_type": "Customer",
					"party": self.customer,
					"debit_in_account_currency": 0,
					"credit_in_account_currency": 1000,
				},
				{
					"account": self.bank,
					"cost_center": self.sub_cc.name,
					"credit_in_account_currency": 0,
					"debit_in_account_currency": 500,
				},
				{
					"account": self.cash,
					"cost_center": self.sub_cc.name,
					"credit_in_account_currency": 0,
					"debit_in_account_currency": 500,
				},
			],
		)
		je_1.submit()

		# make period closing voucher
		pcv = make_period_closing_voucher(
			company=self.company, cost_center=self.cost_center, posting_date=prev_fy_end_date
		)
		pcv.reload()
		# check if period closing voucher is completed
		self.assertEqual(pcv.gle_processing_status, "Completed")

		# make journal entry for active year
		je_2 = self.create_journal_entry(
			acc1=self.debit_to, acc2=self.income_account, amount=1000, posting_date=today()
		)
		je_2.accounts[0].party_type = "Customer"
		je_2.accounts[0].party = self.customer
		je_2.submit()

		# process reconciliation on closed period payment
		pr = self.create_payment_reconciliation(party_is_customer=True)
		pr.from_date = pr.to_date = None
		pr.get_unreconciled_entries()
		pr.allocate_entries()
		pr.reconcile()
		je_1.reload()
		je_2.reload()

		# check whether the payment reconciliation is done on the closed period
		self.assertEqual(pr.get("to_receive"), [])
		self.assertEqual(pr.get("to_pay"), [])

	def test_advance_reconciliation_effect_on_same_date(self):
		frappe.db.set_value(
			"Company",
			self.company,
			{
				"book_advance_payments_in_separate_party_account": 1,
				"default_advance_received_account": self.advance_receivable_account,
				"reconciliation_takes_effect_on": "Reconciliation Date",
			},
		)
		inv_date = convert_to_date(add_days(nowdate(), -1))
		adv_date = convert_to_date(add_days(nowdate(), -2))

		si = self.create_sales_invoice(posting_date=inv_date, qty=1, rate=200)
		pe = self.create_payment_entry(posting_date=adv_date, amount=80).save().submit()

		pr = self.create_payment_reconciliation()
		pr.from_date = add_days(nowdate(), -2)
		pr.to_date = nowdate()
		pr.default_advance_account = self.advance_receivable_account

		# reconcile multiple payments against invoice
		pr.get_unreconciled_entries()
		pr.allocate_entries()

		# Difference amount should not be calculated for base currency accounts
		for row in pr.allocation:
			self.assertEqual(flt(row.get("difference_amount")), 0.0)

		pr.reconcile()

		si.reload()
		self.assertEqual(si.status, "Partly Paid")
		# check PR tool output post reconciliation
		self.assertEqual(len(pr.get("to_receive")), 1)
		self.assertEqual(pr.get("to_receive")[0].get("outstanding_amount"), 120)
		self.assertEqual(pr.get("to_pay"), [])

		# Assert Ledger Entries
		gl_entries = frappe.db.get_all(
			"GL Entry",
			filters={"voucher_no": pe.name},
			fields=["account", "posting_date", "voucher_no", "against_voucher", "debit", "credit"],
			order_by="account, against_voucher, debit",
		)

		expected_gl = [
			{
				"account": self.advance_receivable_account,
				"posting_date": adv_date,
				"voucher_no": pe.name,
				"against_voucher": pe.name,
				"debit": 0.0,
				"credit": 80.0,
			},
			{
				"account": self.advance_receivable_account,
				"posting_date": convert_to_date(nowdate()),
				"voucher_no": pe.name,
				"against_voucher": pe.name,
				"debit": 80.0,
				"credit": 0.0,
			},
			{
				"account": self.debit_to,
				"posting_date": convert_to_date(nowdate()),
				"voucher_no": pe.name,
				"against_voucher": si.name,
				"debit": 0.0,
				"credit": 80.0,
			},
			{
				"account": self.bank,
				"posting_date": adv_date,
				"voucher_no": pe.name,
				"against_voucher": None,
				"debit": 80.0,
				"credit": 0.0,
			},
		]

		self.assertEqual(expected_gl, gl_entries)

		# cancel PE
		pe.reload()
		pe.cancel()
		pr.get_unreconciled_entries()
		# check PR tool output
		self.assertEqual(len(pr.get("to_receive")), 1)
		self.assertEqual(len(pr.get("to_pay")), 0)
		self.assertEqual(pr.get("to_receive")[0].get("outstanding_amount"), 200)

	def test_partial_advance_payment_with_closed_fiscal_year(self):
		"""
		Test Advance Payment partial reconciliation before period closing and partial after period closing
		"""
		default_settings = frappe.db.get_value(
			"Company",
			self.company,
			[
				"book_advance_payments_in_separate_party_account",
				"default_advance_paid_account",
				"reconciliation_takes_effect_on",
			],
			as_dict=True,
		)
		first_fy_start_date = frappe.db.get_value(
			"Fiscal Year", {"disabled": 0}, [{"MIN": "year_start_date"}]
		)
		prev_fy_start_date = add_years(first_fy_start_date, -1)
		prev_fy_end_date = add_days(first_fy_start_date, -1)

		create_fiscal_year(
			company=self.company, year_start_date=prev_fy_start_date, year_end_date=prev_fy_end_date
		)

		frappe.db.set_value(
			"Company",
			self.company,
			{
				"book_advance_payments_in_separate_party_account": 1,
				"default_advance_paid_account": self.advance_payable_account,
				"reconciliation_takes_effect_on": "Oldest Of Invoice Or Advance",
			},
		)

		self.supplier = "_Test Supplier"

		# Create advance payment of 1000 (previous FY)
		pe = self.create_payment_entry(amount=1000, posting_date=prev_fy_start_date)
		pe.party_type = "Supplier"
		pe.party = self.supplier
		pe.payment_type = "Pay"
		pe.paid_from = self.cash
		pe.paid_to = self.advance_payable_account
		pe.save().submit()

		# Create purchase invoice of 600 (previous FY)
		pi1 = self.create_purchase_invoice(qty=1, rate=600, do_not_submit=True)
		pi1.posting_date = prev_fy_start_date
		pi1.set_posting_time = 1
		pi1.supplier = self.supplier
		pi1.credit_to = self.creditors
		pi1.save().submit()

		# Reconcile advance payment
		pr = self.create_payment_reconciliation(party_is_customer=False)
		pr.party = self.supplier
		pr.receivable_payable_account = self.creditors
		pr.default_advance_account = self.advance_payable_account
		pr.from_date = min(pi1.posting_date, pe.posting_date)
		pr.to_date = max(pi1.posting_date, pe.posting_date)
		pr.get_unreconciled_entries()
		to_pay_subset = [x.as_dict() for x in pr.to_pay if x.voucher_no == pi1.name]
		to_receive_subset = [x.as_dict() for x in pr.to_receive if x.voucher_no == pe.name]
		pr.allocate_entries(to_receive=to_receive_subset, to_pay=to_pay_subset)
		pr.reconcile()

		# Verify partial reconciliation
		pe.reload()
		pi1.reload()

		self.assertEqual(len(pe.references), 1)
		self.assertEqual(pe.references[0].allocated_amount, 600)
		self.assertEqual(flt(pe.unallocated_amount), 400)

		self.assertEqual(pi1.outstanding_amount, 0)
		self.assertEqual(pi1.status, "Paid")

		# Close accounting period for March (previous FY)
		pcv = make_period_closing_voucher(
			company=self.company, cost_center=self.cost_center, posting_date=prev_fy_end_date
		)
		pcv.reload()
		self.assertEqual(pcv.gle_processing_status, "Completed")

		# Change reconciliation setting to "Reconciliation Date"
		frappe.db.set_value(
			"Company",
			self.company,
			"reconciliation_takes_effect_on",
			"Reconciliation Date",
		)

		# Create new purchase invoice for 400 in new fiscal year
		pi2 = self.create_purchase_invoice(qty=1, rate=400, do_not_submit=True)
		pi2.posting_date = today()
		pi2.set_posting_time = 1
		pi2.supplier = self.supplier
		pi2.currency = "INR"
		pi2.credit_to = self.creditors
		pi2.save()
		pi2.submit()

		# Allocate 600 from advance payment to purchase invoice
		pr = self.create_payment_reconciliation(party_is_customer=False)
		pr.party = self.supplier
		pr.receivable_payable_account = self.creditors
		pr.default_advance_account = self.advance_payable_account
		pr.from_date = min(getdate(pi2.posting_date), getdate(pe.posting_date))
		pr.to_date = max(getdate(pi2.posting_date), getdate(pe.posting_date))
		pr.get_unreconciled_entries()
		to_pay_subset = [x.as_dict() for x in pr.to_pay if x.voucher_no == pi2.name]
		to_receive_subset = [x.as_dict() for x in pr.to_receive if x.voucher_no == pe.name]
		pr.allocate_entries(to_receive=to_receive_subset, to_pay=to_pay_subset)
		pr.reconcile()

		pe.reload()
		pi2.reload()

		# Assert advance payment is fully allocated
		self.assertEqual(len(pe.references), 2)
		self.assertEqual(flt(pe.unallocated_amount), 0)

		# Assert new invoice is fully paid
		self.assertEqual(pi2.outstanding_amount, 0)
		self.assertEqual(pi2.status, "Paid")

		# Verify reconciliation dates are correct based on company setting
		self.assertEqual(getdate(pe.references[0].reconcile_effect_on), getdate(pi1.posting_date))
		self.assertEqual(getdate(pe.references[1].reconcile_effect_on), getdate(pi2.posting_date))

		frappe.db.set_value("Company", self.company, default_settings)

	def test_foreign_currency_reverse_payment_entry_against_payment_entry_for_customer(self):
		transaction_date = nowdate()
		customer = self.customer3
		amount = 1000
		exchange_rate_at_payment = 100
		exchange_rate_at_reverse_payment = 95

		# Receive amount from customer - 1,00,000
		pe = self.create_payment_entry(amount=amount, posting_date=transaction_date, customer=customer)
		pe.payment_type = "Receive"
		pe.paid_from = self.debtors_eur
		pe.paid_from_account_currency = "EUR"
		pe.source_exchange_rate = exchange_rate_at_payment
		pe.paid_amount = amount
		pe.received_amount = exchange_rate_at_payment * amount
		pe.paid_to = self.cash
		pe.paid_to_account_currency = "INR"
		pe = pe.save().submit()

		# Pay amount to customer - 95,000
		reverse_pe = self.create_payment_entry(
			amount=amount, posting_date=transaction_date, customer=customer
		)
		reverse_pe.payment_type = "Pay"
		reverse_pe.paid_from = self.cash
		reverse_pe.paid_from_account_currency = "INR"
		reverse_pe.target_exchange_rate = exchange_rate_at_reverse_payment
		reverse_pe.paid_amount = exchange_rate_at_reverse_payment * amount
		reverse_pe.received_amount = amount
		reverse_pe.paid_to = self.debtors_eur
		reverse_pe.paid_to_account_currency = "EUR"
		reverse_pe.save().submit()

		# Reconcile payments
		pr = self.create_payment_reconciliation()
		pr.party = customer
		pr.receivable_payable_account = self.debtors_eur
		pr.get_unreconciled_entries()
		self.assertEqual(len(pr.get("to_receive")), 1)
		self.assertEqual(len(pr.get("to_pay")), 1)
		pr.allocate_entries()

		# Check the difference_amount is a gain of 5000
		self.assertEqual(flt(pr.allocation[0].get("difference_amount")), 5000.0)
		pr.reconcile()

	def test_foreign_currency_reverse_payment_entry_against_payment_entry_for_supplier(self):
		transaction_date = nowdate()
		self.supplier = "_Test Supplier USD"
		amount = 1000
		exchange_rate_at_payment = 100
		exchange_rate_at_reverse_payment = 95

		# Pay amount to supplier - 1,00,000
		pe = self.create_payment_entry(amount=amount, posting_date=transaction_date)
		pe.payment_type = "Pay"
		pe.party_type = "Supplier"
		pe.party = self.supplier
		pe.paid_from = self.cash
		pe.paid_from_account_currency = "INR"
		pe.target_exchange_rate = exchange_rate_at_payment
		pe.paid_amount = exchange_rate_at_payment * amount
		pe.received_amount = amount
		pe.paid_to = self.creditors_usd
		pe.paid_to_account_currency = "USD"
		pe.save().submit()

		# Receive amount from supplier - 95,000
		reverse_pe = self.create_payment_entry(amount=amount, posting_date=transaction_date)
		reverse_pe.payment_type = "Receive"
		reverse_pe.party_type = "Supplier"
		reverse_pe.party = self.supplier
		reverse_pe.paid_from = self.creditors_usd
		reverse_pe.paid_from_account_currency = "USD"
		reverse_pe.source_exchange_rate = exchange_rate_at_reverse_payment
		reverse_pe.paid_amount = amount
		reverse_pe.received_amount = exchange_rate_at_reverse_payment * amount
		reverse_pe.paid_to = self.cash
		reverse_pe.paid_to_account_currency = "INR"
		reverse_pe = reverse_pe.save().submit()

		# Reconcile payments
		pr = self.create_payment_reconciliation(party_is_customer=False)
		pr.party = self.supplier
		pr.receivable_payable_account = self.creditors_usd
		pr.get_unreconciled_entries()

		self.assertEqual(len(pr.get("to_receive")), 1)
		self.assertEqual(len(pr.get("to_pay")), 1)
		pr.allocate_entries()

		# Check the difference_amount is a loss of 5000
		self.assertEqual(flt(pr.allocation[0].get("difference_amount")), -5000.0)
		pr.reconcile()

	def test_foreign_currency_reverse_journal_entry_against_journal_entry_for_customer(self):
		transaction_date = nowdate()
		customer = self.customer3
		amount = 1000
		exchange_rate_at_payment = 95
		exchange_rate_at_reverse_payment = 100

		# Receive amount from customer - 95,000
		je1 = self.create_journal_entry(self.cash, self.debtors_eur, amount, transaction_date)
		je1.multi_currency = 1
		je1.accounts[0].exchange_rate = 1
		je1.accounts[0].debit_in_account_currency = exchange_rate_at_payment * amount
		je1.accounts[0].debit = exchange_rate_at_payment * amount
		je1.accounts[1].party_type = "Customer"
		je1.accounts[1].party = customer
		je1.accounts[1].exchange_rate = exchange_rate_at_payment
		je1.accounts[1].credit_in_account_currency = amount
		je1.accounts[1].credit = exchange_rate_at_payment * amount
		je1.save()
		je1.submit()

		# Pay amount to customer - 1,00,000
		je2 = self.create_journal_entry(self.debtors_eur, self.cash, amount, transaction_date)
		je2.multi_currency = 1
		je2.accounts[0].party_type = "Customer"
		je2.accounts[0].party = customer
		je2.accounts[0].exchange_rate = exchange_rate_at_reverse_payment
		je2.accounts[0].debit_in_account_currency = amount
		je2.accounts[0].debit = exchange_rate_at_reverse_payment * amount
		je2.accounts[1].exchange_rate = 1
		je2.accounts[1].credit_in_account_currency = exchange_rate_at_reverse_payment * amount
		je2.accounts[1].credit = exchange_rate_at_reverse_payment * amount
		je2.save()
		je2.submit()

		# Reconcile payments
		pr = self.create_payment_reconciliation()
		pr.party = customer
		pr.receivable_payable_account = self.debtors_eur
		pr.get_unreconciled_entries()

		self.assertEqual(len(pr.to_receive), 1)
		self.assertEqual(len(pr.to_pay), 1)

		pr.allocate_entries()

		# Check the difference_amount is a loss of 5000
		self.assertEqual(flt(pr.allocation[0].difference_amount), -5000.0)
		pr.reconcile()

	def test_foreign_currency_reverse_journal_entry_against_journal_entry_for_supplier(self):
		transaction_date = nowdate()
		self.supplier = "_Test Supplier USD"
		amount = 1000
		exchange_rate_at_payment = 95
		exchange_rate_at_reverse_payment = 100

		# Pay amount to supplier - 95,000
		je1 = self.create_journal_entry(self.creditors_usd, self.cash, amount, transaction_date)
		je1.multi_currency = 1
		je1.accounts[0].party_type = "Supplier"
		je1.accounts[0].party = self.supplier
		je1.accounts[0].exchange_rate = exchange_rate_at_payment
		je1.accounts[0].debit_in_account_currency = amount
		je1.accounts[0].debit = exchange_rate_at_payment * amount
		je1.accounts[1].exchange_rate = 1
		je1.accounts[1].credit = exchange_rate_at_payment * amount
		je1.accounts[1].credit_in_account_currency = exchange_rate_at_payment * amount
		je1.save()
		je1.submit()

		# Receive amount from supplier - 1,00,000
		je2 = self.create_journal_entry(self.cash, self.creditors_usd, amount, transaction_date)
		je2.multi_currency = 1
		je2.accounts[0].exchange_rate = 1
		je2.accounts[0].debit = exchange_rate_at_reverse_payment * amount
		je2.accounts[0].debit_in_account_currency = exchange_rate_at_reverse_payment * amount
		je2.accounts[1].party_type = "Supplier"
		je2.accounts[1].party = self.supplier
		je2.accounts[1].exchange_rate = exchange_rate_at_reverse_payment
		je2.accounts[1].credit_in_account_currency = amount
		je2.accounts[1].credit = exchange_rate_at_reverse_payment * amount
		je2.save()
		je2.submit()

		# Reconcile payments
		pr = self.create_payment_reconciliation()
		pr.party_type = "Supplier"
		pr.party = self.supplier
		pr.receivable_payable_account = self.creditors_usd
		pr.get_unreconciled_entries()

		self.assertEqual(len(pr.to_receive), 1)
		self.assertEqual(len(pr.to_pay), 1)

		pr.allocate_entries()

		# Check the difference_amount is a gain of 5000
		self.assertEqual(flt(pr.allocation[0].difference_amount), 5000.0)
		pr.reconcile()

	# ----------------------------------------------------------------
	# Phase 1 — new capabilities
	# ----------------------------------------------------------------

	def test_cross_account_fetch_no_account_filter(self):
		"""Receivable/Payable account left blank → fetcher returns rows from all party
		accounts of the natural side. SIs on default Debtors AND on Debtors-EUR should
		both appear in `to_receive`.
		"""
		# Default-currency SI on Debtors
		self.create_sales_invoice(qty=1, rate=100)

		# EUR SI on Debtors-EUR — uses customer3 which has EUR default currency
		si_eur = create_sales_invoice(
			qty=1,
			rate=50,
			company=self.company,
			customer=self.customer3,
			item_code=self.item,
			cost_center=self.cost_center,
			warehouse=self.warehouse,
			debit_to=self.debtors_eur,
			parent_cost_center=self.cost_center,
			update_stock=0,
			currency="EUR",
			conversion_rate=85,
			is_pos=0,
			is_return=0,
			income_account=self.income_account,
			expense_account=self.expense_account,
		)

		pr = frappe.new_doc("Payment Reconciliation")
		pr.company = self.company
		pr.party_type = "Customer"
		pr.party = self.customer  # Default-currency customer with the INR SI
		# Leave receivable_payable_account blank → cross-account fetch
		pr.from_date = pr.to_date = nowdate()
		pr.get_unreconciled_entries()

		# Default customer's SI appears (not EUR SI which belongs to customer3).
		# Cross-account mode still scopes by party, just fetches from all accounts the
		# party has activity on.
		self.assertGreaterEqual(len(pr.to_receive), 1)

		# Same fetcher run for the EUR customer should pick up the EUR SI.
		pr_eur = frappe.new_doc("Payment Reconciliation")
		pr_eur.company = self.company
		pr_eur.party_type = "Customer"
		pr_eur.party = self.customer3
		pr_eur.from_date = pr_eur.to_date = nowdate()
		pr_eur.get_unreconciled_entries()
		self.assertEqual(len(pr_eur.to_receive), 1)
		self.assertEqual(pr_eur.to_receive[0].voucher_no, si_eur.name)
		self.assertEqual(pr_eur.to_receive[0].account, self.debtors_eur)

	def test_je_both_directions_routed(self):
		"""After PR-B both JE directions are fetched and routed by Classifier.

		JE Cr-to-Debtors (Customer payment-like) → to_pay (Receivable, -ve).
		JE Dr-to-Debtors (Customer invoice-like) → to_receive (Receivable, +ve).
		The against-side update is netted per (JE, account) from PLE in
		`OpenBalanceFetcher._query_je_outstanding`.
		"""
		# Cr Debtors: payment-like.
		je_payment_like = self.create_journal_entry(self.debit_to, self.bank, -150)
		je_payment_like.accounts[0].party_type = "Customer"
		je_payment_like.accounts[0].party = self.customer
		je_payment_like.save().submit()

		# Dr Debtors: invoice-like.
		je_invoice_like = self.create_journal_entry(self.debit_to, self.bank, 200)
		je_invoice_like.accounts[0].party_type = "Customer"
		je_invoice_like.accounts[0].party = self.customer
		je_invoice_like.save().submit()

		pr = self.create_payment_reconciliation()
		pr.get_unreconciled_entries()

		cr_je_rows = [r for r in pr.to_pay if r.voucher_no == je_payment_like.name]
		self.assertEqual(len(cr_je_rows), 1)

		dr_je_rows = [r for r in pr.to_receive if r.voucher_no == je_invoice_like.name]
		self.assertEqual(len(dr_je_rows), 1)

	def test_currency_filter_restricts_fetch(self):
		"""`currency_filter` restricts both tables to one currency."""
		self.create_sales_invoice(qty=1, rate=100)  # INR
		self.create_payment_entry(amount=100).save().submit()  # INR

		# Add an EUR SI for a different customer
		si_eur = create_sales_invoice(
			qty=1,
			rate=50,
			company=self.company,
			customer=self.customer3,
			item_code=self.item,
			cost_center=self.cost_center,
			warehouse=self.warehouse,
			debit_to=self.debtors_eur,
			parent_cost_center=self.cost_center,
			update_stock=0,
			currency="EUR",
			conversion_rate=85,
			is_return=0,
			income_account=self.income_account,
			expense_account=self.expense_account,
		)

		pr_eur = frappe.new_doc("Payment Reconciliation")
		pr_eur.company = self.company
		pr_eur.party_type = "Customer"
		pr_eur.party = self.customer3
		pr_eur.currency_filter = "EUR"
		pr_eur.from_date = pr_eur.to_date = nowdate()
		pr_eur.get_unreconciled_entries()
		# Only EUR rows.
		self.assertTrue(all(r.currency == "EUR" for r in pr_eur.to_receive))
		self.assertEqual(len(pr_eur.to_receive), 1)
		self.assertEqual(pr_eur.to_receive[0].voucher_no, si_eur.name)

	def test_manual_allocation_blocked_on_currency_mismatch(self):
		"""validate_allocation rejects rows pairing entries from different currency
		buckets. Auto-Match's bucketing prevents this; the validator guards manual
		edits / future cross-party flows that might assemble rows from different
		buckets."""
		pr = self.create_payment_reconciliation()

		# Hand-roll mismatched-currency rows. validate_allocation looks up source
		# rows by (voucher_type, voucher_no, voucher_row); the voucher_no values
		# don't have to be real submitted docs because we only exercise validate.
		pr.append(
			"to_receive",
			{
				"voucher_type": "Sales Invoice",
				"voucher_no": "SI-INR-FAKE",
				"outstanding_amount": 100,
				"currency": "INR",
				"party_type": "Customer",
				"party": self.customer,
				"account": self.debit_to,
			},
		)
		pr.append(
			"to_pay",
			{
				"voucher_type": "Payment Entry",
				"voucher_no": "PE-EUR-FAKE",
				"outstanding_amount": 100,
				"currency": "EUR",
				"party_type": "Customer",
				"party": self.customer,
				"account": self.debtors_eur,
			},
		)
		pr.append(
			"allocation",
			{
				"to_receive_voucher_type": "Sales Invoice",
				"to_receive_voucher_no": "SI-INR-FAKE",
				"to_pay_voucher_type": "Payment Entry",
				"to_pay_voucher_no": "PE-EUR-FAKE",
				"allocated_amount": 100,
				"to_receive_party_type": "Customer",
				"to_receive_party": self.customer,
				"to_pay_party_type": "Customer",
				"to_pay_party": self.customer,
			},
		)

		with self.assertRaises(frappe.ValidationError) as cm:
			pr.validate_allocation()
		self.assertIn("Cross-currency", str(cm.exception))

	def test_je_split_into_both_tables(self):
		"""Single Journal Entry with one Dr-to-Debtors row AND one Cr-to-Debtors
		row for the same customer should produce one row per side: the Dr row in
		`to_receive`, the Cr row in `to_pay`. PLE nets (JE, Debtors) to 600
		(1000 - 400); with nothing settled against the JE, `_query_je_outstanding`
		emits each JEA row at its full signed balance (Dr 1000, Cr 400)."""
		# Build a JE with three accounts: Dr Debtors 1000, Cr Debtors 400, Cr Sales 600
		je = frappe.new_doc("Journal Entry")
		je.posting_date = nowdate()
		je.company = self.company
		je.user_remark = "split JE test"
		je.append(
			"accounts",
			{
				"account": self.debit_to,
				"party_type": "Customer",
				"party": self.customer,
				"debit_in_account_currency": 1000,
				"cost_center": self.cost_center,
			},
		)
		je.append(
			"accounts",
			{
				"account": self.debit_to,
				"party_type": "Customer",
				"party": self.customer,
				"credit_in_account_currency": 400,
				"cost_center": self.cost_center,
			},
		)
		je.append(
			"accounts",
			{
				"account": self.income_account,
				"credit_in_account_currency": 600,
				"cost_center": self.cost_center,
			},
		)
		je.save().submit()

		pr = self.create_payment_reconciliation()
		pr.get_unreconciled_entries()

		dr_rows = [r for r in pr.to_receive if r.voucher_no == je.name]
		cr_rows = [r for r in pr.to_pay if r.voucher_no == je.name]
		self.assertEqual(len(dr_rows), 1, "Dr-to-Debtors leg should land in to_receive")
		self.assertEqual(len(cr_rows), 1, "Cr-to-Debtors leg should land in to_pay")
		self.assertEqual(flt(dr_rows[0].outstanding_amount), 1000)
		self.assertEqual(flt(cr_rows[0].outstanding_amount), 400)

	def test_je_multi_account_same_party(self):
		"""Reverse JE (credits the customer) on TWO receivable accounts, then a
		partial Pay PE settles the FIRST account via reconciliation.

		PLE's voucher-outstanding CTE groups by (against_voucher, party) without
		account, so the JE collapses to one net figure across both accounts; the
		fetcher must still recover per-account row identity from the JEA rows.

		The re-fetch must show the first account net of the PE and the second
		account at full balance — the FIFO `settled` drain attributes the PE→JE
		settlement to the lowest-idx JEA row (here the account the PE paid). It
		also guards against a premature `break`: the unsettled second leg sits
		after the drained first leg and must still be emitted."""
		je = frappe.new_doc("Journal Entry")
		je.posting_date = nowdate()
		je.company = self.company
		je.user_remark = "reverse multi-account JE test"
		je.append(
			"accounts",
			{
				"account": self.debit_to,
				"party_type": "Customer",
				"party": self.customer,
				"credit_in_account_currency": 1000,
				"cost_center": self.cost_center,
			},
		)
		je.append(
			"accounts",
			{
				"account": self.advance_receivable_account,
				"party_type": "Customer",
				"party": self.customer,
				"credit_in_account_currency": 500,
				"cost_center": self.cost_center,
			},
		)
		je.append(
			"accounts",
			{
				"account": self.bank,
				"debit_in_account_currency": 1500,
				"cost_center": self.cost_center,
			},
		)
		je.save().submit()

		# Pay PE (refund) Dr-s the second receivable account → to_receive (+400).
		pe = create_payment_entry(
			company=self.company,
			payment_type="Pay",
			party_type="Customer",
			party=self.customer,
			paid_from=self.bank,
			paid_to=self.advance_receivable_account,
			paid_amount=400,
		)
		pe.save().submit()

		pr = self.create_payment_reconciliation()
		pr.get_unreconciled_entries()

		# Both JE credit legs are payment-like → to_pay; the Pay PE is to_receive.
		je_pay = {r.account: flt(r.outstanding_amount) for r in pr.to_pay if r.voucher_no == je.name}
		self.assertEqual(je_pay.get(self.debit_to), 1000)
		self.assertEqual(je_pay.get(self.advance_receivable_account), 500)
		self.assertTrue(any(r.voucher_no == pe.name for r in pr.to_receive))

		pr.allocate_entries()
		pr.reconcile()

		# After settling 400 of the second account: first account 1000, second 100.
		je_pay = {r.account: flt(r.outstanding_amount) for r in pr.to_pay if r.voucher_no == je.name}
		self.assertEqual(je_pay.get(self.debit_to), 1000)
		self.assertEqual(je_pay.get(self.advance_receivable_account), 100)
		self.assertFalse(any(r.voucher_no == pe.name for r in pr.to_receive))

	def test_allocator_never_crosses_currency(self):
		"""Allocator buckets by currency. Direct invocation with mixed-currency
		hand-rolled rows must produce only same-currency allocations.

		(Driving this through real submitted vouchers is awkward — Frappe's
		`validate_party_gle_currency` rejects cross-currency entries for one
		party, and Allocator's bucketing is per-PR-doc which is per-party. So
		the bucketing is meaningfully tested by hand-feeding the pure-function
		Allocator with rows from different currency buckets.)
		"""
		from erpnext.accounts.doctype.payment_reconciliation.payment_reconciliation import (
			Allocator,
		)

		pr = self.create_payment_reconciliation()

		def _row(voucher_no, amount, currency, account):
			return frappe._dict(
				{
					"voucher_type": "Sales Invoice",
					"voucher_no": voucher_no,
					"voucher_row": None,
					"outstanding_amount": amount,
					"amount": amount,
					"currency": currency,
					"exchange_rate": 85 if currency == "EUR" else 1,
					"posting_date": nowdate(),
					"account": account,
					"account_type": "Receivable",
					"party_type": "Customer",
					"party": self.customer,
					"is_advance": 0,
					"is_return": 0,
					"cost_center": self.cost_center,
				}
			)

		def _pay_row(voucher_no, amount, currency, account):
			row = _row(voucher_no, amount, currency, account)
			row.voucher_type = "Payment Entry"
			return row

		to_receive = [
			_row("SI-INR", 500, "INR", self.debit_to),
			_row("SI-EUR", 100, "EUR", self.debtors_eur),
		]
		to_pay = [
			_pay_row("PE-EUR", 100, "EUR", self.debtors_eur),
			_pay_row("PE-INR", 500, "INR", self.debit_to),
		]

		allocs = Allocator(pr, to_receive=to_receive, to_pay=to_pay).allocate()

		# Two allocations, one per currency bucket. NEVER cross-currency.
		self.assertEqual(len(allocs), 2)
		pairs = {(a["to_receive_voucher_no"], a["to_pay_voucher_no"], a["currency"]) for a in allocs}
		self.assertIn(("SI-INR", "PE-INR", "INR"), pairs)
		self.assertIn(("SI-EUR", "PE-EUR", "EUR"), pairs)

	def test_get_open_balances_for_voucher_direct_api(self):
		"""`get_open_balances_for_voucher` is the direct-call entrypoint for the
		"reconcile against opposite" UX. Given a SI, it instantiates a virtual
		PR doc internally and returns `to_receive` / `to_pay` for the same party.
		"""
		from erpnext.accounts.doctype.payment_reconciliation.payment_reconciliation import (
			get_open_balances_for_voucher,
		)

		si = self.create_sales_invoice(qty=1, rate=200)
		pe = self.create_payment_entry(amount=200)
		pe.save().submit()

		balances = get_open_balances_for_voucher("Sales Invoice", si.name)
		recv_nos = {r["voucher_no"] for r in balances["to_receive"]}
		pay_nos = {r["voucher_no"] for r in balances["to_pay"]}
		# SI lands on to_receive (open receivable); unallocated PE Receive lands on to_pay.
		self.assertIn(si.name, recv_nos)
		self.assertIn(pe.name, pay_nos)

	# ------------------------------------------------------------------
	# Regression: three reconcile-router bugs (cases 1, 2, 3)
	# ------------------------------------------------------------------

	def test_supplier_journal_against_journal_same_account(self):
		"""Case 1 (same-account JE doubling).

		A Supplier invoice-like JE (Cr Creditors) reconciled against a
		payment-like JE (Dr Creditors) on the SAME account. The invoice-like JE
		is picked as the voucher; its party leg is a credit, i.e. opposite the
		party-wide dr_or_cr (debit) — the exact condition that made the JEA
		splitter double the leg (1000 -> 2000) instead of settling it. Both JEs
		must fully reconcile.
		"""
		self.supplier = make_supplier("_Test PR Supplier INR")

		# invoice-like JE: Dr Expense 1000 / Cr Creditors 1000
		je_inv = self.create_journal_entry(self.expense_account, self.creditors, 1000)
		je_inv.accounts[1].party_type = "Supplier"
		je_inv.accounts[1].party = self.supplier
		je_inv.save().submit()

		# payment-like JE: Dr Creditors 1000 / Cr Bank 1000
		je_pay = self.create_journal_entry(self.creditors, self.bank, 1000)
		je_pay.accounts[0].party_type = "Supplier"
		je_pay.accounts[0].party = self.supplier
		je_pay.save().submit()

		pr = self.create_payment_reconciliation(party_is_customer=False)
		pr.party = self.supplier
		pr.receivable_payable_account = self.creditors
		pr.get_unreconciled_entries()

		self.assertEqual(len(pr.get("to_pay")), 1)  # invoice-like
		self.assertEqual(len(pr.get("to_receive")), 1)  # payment-like
		pr.allocate_entries()
		pr.reconcile()

		# Both legs fully settled — nothing left to reconcile.
		remaining = {r.voucher_no for r in pr.get("to_receive")} | {r.voucher_no for r in pr.get("to_pay")}
		self.assertNotIn(je_inv.name, remaining)
		self.assertNotIn(je_pay.name, remaining)

	def test_debit_note_against_opposite_journal(self):
		"""Case 3 (PI return / debit note reconciled against an opposite JE).

		A Purchase Invoice return (debit note) sits on to_receive; an opposite
		invoice-like JE (Cr Creditors) sits on to_pay. The JE is picked as the
		voucher with its party leg on the credit side — the same opposite-leg
		condition as case 1 — and must not double on split. INR throughout to
		isolate the doubling from FX (case 2 covers FX).
		"""
		self.supplier = make_supplier("_Test PR Supplier INR")

		pi = self.create_purchase_invoice(qty=2, rate=50, do_not_submit=True)
		pi.supplier = self.supplier
		pi.credit_to = self.creditors
		pi.save().submit()

		pi_return = frappe.get_doc(pi.as_dict())
		pi_return.name = None
		pi_return.docstatus = 0
		pi_return.is_return = 1
		pi_return.return_against = pi.name
		pi_return.items[0].qty = -pi_return.items[0].qty
		pi_return.save().submit()

		# invoice-like JE: Dr Expense 100 / Cr Creditors 100 (opposite leg)
		je = self.create_journal_entry(self.expense_account, self.creditors, 100)
		je.accounts[1].party_type = "Supplier"
		je.accounts[1].party = self.supplier
		je.save().submit()

		pr = self.create_payment_reconciliation(party_is_customer=False)
		pr.party = self.supplier
		pr.receivable_payable_account = self.creditors
		pr.get_unreconciled_entries()

		dn_rows = [r.as_dict() for r in pr.get("to_receive") if r.voucher_no == pi_return.name]
		je_rows = [r.as_dict() for r in pr.get("to_pay") if r.voucher_no == je.name]
		self.assertEqual(len(dn_rows), 1)
		self.assertEqual(len(je_rows), 1)

		pr.allocate_entries(to_receive=dn_rows, to_pay=je_rows)
		pr.reconcile()

		# DN and JE both fully settled.
		remaining = {r.voucher_no for r in pr.get("to_receive")} | {r.voucher_no for r in pr.get("to_pay")}
		self.assertNotIn(pi_return.name, remaining)
		self.assertNotIn(je.name, remaining)

		# Same-account note↔JE settles via the normal split flow — NO bridge JE.
		self.assertFalse(
			frappe.db.exists(
				"Journal Entry", {"company": self.company, "voucher_type": "Reconciliation Journal"}
			)
		)

	def test_purchase_return_against_payment_entry(self):
		"""Scenario 1: a Purchase Invoice return (debit note) reconciled against an
		opposite Receive PE in the SAME account. Settled by the normal PE flow (the
		PE references the DN); NO cross-account bridge JE."""
		self.supplier = make_supplier("_Test PR Supplier INR")

		pi = self.create_purchase_invoice(qty=2, rate=50, do_not_submit=True)
		pi.supplier = self.supplier
		pi.credit_to = self.creditors
		pi.save().submit()

		dn = frappe.get_doc(pi.as_dict())
		dn.name = None
		dn.docstatus = 0
		dn.is_return = 1
		dn.return_against = pi.name
		dn.items[0].qty = -dn.items[0].qty
		dn.save().submit()

		# Receive PE refunding the return: Cr Creditors (credit balance → to_pay).
		pe = self.create_payment_entry(amount=100)
		pe.payment_type = "Receive"
		pe.party_type = "Supplier"
		pe.party = self.supplier
		pe.paid_from = self.creditors
		pe.paid_to = self.bank
		pe.save().submit()

		pr = self.create_payment_reconciliation(party_is_customer=False)
		pr.party = self.supplier
		pr.receivable_payable_account = self.creditors
		pr.get_unreconciled_entries()

		dn_rows = [r.as_dict() for r in pr.get("to_receive") if r.voucher_no == dn.name]
		pe_rows = [r.as_dict() for r in pr.get("to_pay") if r.voucher_no == pe.name]
		self.assertEqual(len(dn_rows), 1)
		self.assertEqual(len(pe_rows), 1)

		pr.allocate_entries(to_receive=dn_rows, to_pay=pe_rows)
		self.assertFalse(any(r.is_cross_account for r in pr.allocation))
		pr.reconcile()

		remaining = {r.voucher_no for r in pr.get("to_receive")} | {r.voucher_no for r in pr.get("to_pay")}
		self.assertNotIn(dn.name, remaining)
		self.assertNotIn(pe.name, remaining)
		self.assertFalse(
			frappe.db.exists(
				"Journal Entry", {"company": self.company, "voucher_type": "Reconciliation Journal"}
			)
		)

	def test_note_usd_against_payment_entry_rate_change(self):
		"""Scenario 3: a USD Purchase return (debit note) reconciled against a USD
		Receive PE booked at a DIFFERENT rate, SAME account. Settled by the normal PE
		flow with the FX difference booked; NO cross-account bridge JE."""
		self.supplier = make_supplier("_Test Supplier USD", "USD")
		amount = 100
		rate_dn = 80
		rate_pe = 83

		pi = self.create_purchase_invoice(qty=2, rate=50, do_not_submit=True)
		pi.supplier = self.supplier
		pi.currency = "USD"
		pi.conversion_rate = rate_dn
		pi.credit_to = self.creditors_usd
		pi.save().submit()

		dn = frappe.get_doc(pi.as_dict())
		dn.name = None
		dn.docstatus = 0
		dn.is_return = 1
		dn.return_against = pi.name
		dn.items[0].qty = -dn.items[0].qty
		dn.save().submit()

		# Receive PE in USD at a different rate.
		pe = self.create_payment_entry(amount=amount)
		pe.payment_type = "Receive"
		pe.party_type = "Supplier"
		pe.party = self.supplier
		pe.paid_from = self.creditors_usd
		pe.paid_to = self.cash
		pe.paid_from_account_currency = "USD"
		pe.source_exchange_rate = rate_pe
		pe.received_amount = amount * rate_pe
		pe.save().submit()

		pr = self.create_payment_reconciliation(party_is_customer=False)
		pr.party = self.supplier
		pr.receivable_payable_account = self.creditors_usd
		pr.get_unreconciled_entries()

		dn_rows = [r.as_dict() for r in pr.get("to_receive") if r.voucher_no == dn.name]
		pe_rows = [r.as_dict() for r in pr.get("to_pay") if r.voucher_no == pe.name]
		self.assertEqual(len(dn_rows), 1)
		self.assertEqual(len(pe_rows), 1)

		pr.allocate_entries(to_receive=dn_rows, to_pay=pe_rows)
		self.assertFalse(any(r.is_cross_account for r in pr.allocation))
		pr.reconcile()

		# Settled, no bridge JE; FX difference booked via the standard flow.
		self.assertFalse(
			frappe.db.exists(
				"Journal Entry", {"company": self.company, "voucher_type": "Reconciliation Journal"}
			)
		)
		dn.reload()
		self.assertEqual(flt(dn.outstanding_amount), 0)

	def test_note_usd_against_payment_entry_rate_change_cross_account(self):
		"""Cross-account mirror of `test_note_usd_against_payment_entry_rate_change`:
		a USD Purchase return (debit note) in one payable account reconciled against a
		USD Receive PE in a DIFFERENT payable account, booked at another rate. Now
		auto-bridged — a 'Reconciliation Journal' settles both and the FX difference is
		booked through the standard `reconcile_against_document` flow (no separate
		`reconcile_dr_cr_note` path)."""
		self.supplier = make_supplier("_Test Supplier USD", "USD")
		payable_usd2 = self._make_account(
			"Payable USD 2", "Accounts Payable - _PR", "Payable", currency="USD"
		)
		amount = 100
		rate_dn = 80
		rate_pe = 83

		pi = self.create_purchase_invoice(qty=2, rate=50, do_not_submit=True)
		pi.supplier = self.supplier
		pi.currency = "USD"
		pi.conversion_rate = rate_dn
		pi.credit_to = self.creditors_usd
		pi.save().submit()

		dn = frappe.get_doc(pi.as_dict())
		dn.name = None
		dn.docstatus = 0
		dn.is_return = 1
		dn.return_against = pi.name
		dn.items[0].qty = -dn.items[0].qty
		dn.save().submit()

		# Receive PE in USD at a different rate, booked in a DIFFERENT payable account.
		pe = self.create_payment_entry(amount=amount)
		pe.payment_type = "Receive"
		pe.party_type = "Supplier"
		pe.party = self.supplier
		pe.paid_from = payable_usd2
		pe.paid_to = self.cash
		pe.paid_from_account_currency = "USD"
		pe.source_exchange_rate = rate_pe
		pe.received_amount = amount * rate_pe
		pe.save().submit()

		pr = self._cross_account_pr(self.supplier, party_is_customer=False)
		pr.currency_filter = "USD"
		pr.get_unreconciled_entries()

		dn_rows = [r.as_dict() for r in pr.get("to_receive") if r.voucher_no == dn.name]
		pe_rows = [r.as_dict() for r in pr.get("to_pay") if r.voucher_no == pe.name]
		self.assertEqual(len(dn_rows), 1)
		self.assertEqual(len(pe_rows), 1)
		# The two sit in different party accounts → this is a cross-account pair.
		self.assertNotEqual(dn_rows[0]["account"], pe_rows[0]["account"])

		pr.allocate_entries(to_receive=dn_rows, to_pay=pe_rows)
		self.assertTrue(any(r.is_cross_account for r in pr.allocation))
		# Different rates → FX difference 100 * (80 - 83) on the payable leg.
		self.assertEqual(flt(pr.allocation[0].difference_amount), -300)

		jes_before = {j.name for j in frappe.get_all("Journal Entry")}
		pr.reconcile()
		new_jes = frappe.get_all(
			"Journal Entry",
			filters={"name": ["not in", list(jes_before) or [""]]},
			fields=["voucher_type"],
		)
		by_type = {}
		for j in new_jes:
			by_type[j.voucher_type] = by_type.get(j.voucher_type, 0) + 1
		# Exactly two JEs: ONE bridge transfer (moves value across the two payable
		# accounts — minted once per allocation row, NOT once per split sub-row) and
		# ONE FX gain/loss JE for the rate difference.
		self.assertEqual(by_type.get("Reconciliation Journal"), 1)
		self.assertEqual(by_type.get("Exchange Gain Or Loss"), 1)
		self.assertEqual(len(new_jes), 2)

		# Note settled; PE consumed — neither reappears for the party.
		dn.reload()
		self.assertEqual(flt(dn.outstanding_amount), 0)
		pr2 = self._cross_account_pr(self.supplier, party_is_customer=False)
		pr2.currency_filter = "USD"
		pr2.get_unreconciled_entries()
		self.assertNotIn(dn.name, {r.voucher_no for r in pr2.to_receive})
		self.assertNotIn(pe.name, {r.voucher_no for r in pr2.to_pay})

	def test_cr_note_against_reverse_payment_entry(self):
		"""A customer credit note (return) reconciled against a REVERSE (Pay) Payment
		Entry — a refund — in the SAME debtors account. The PE is writable, so this is
		settled DIRECTLY with NO bridge: the reference is written on the PE's
		`references` table pointing at the credit note (via `reconcile_against_document`
		→ `update_reference_in_payment_entry`). Contrast the cross-account case, where
		the PE references a bridge JE and the bridge's JEA references the note."""
		transaction_date = nowdate()
		amount = 100

		cr_note = self.create_sales_invoice(
			qty=-1, rate=amount, posting_date=transaction_date, do_not_save=True, do_not_submit=True
		)
		cr_note.is_return = 1
		cr_note = cr_note.save().submit()

		# Reverse (Pay) PE = refund to the customer: Dr Debtors.
		pe = self.create_payment_entry(amount=amount)
		pe.payment_type = "Pay"
		pe.paid_from = self.bank
		pe.paid_to = self.debit_to
		pe.save().submit()

		pr = self.create_payment_reconciliation()
		pr.get_unreconciled_entries()

		cn_rows = [r.as_dict() for r in pr.get("to_pay") if r.voucher_no == cr_note.name]
		pe_rows = [r.as_dict() for r in pr.get("to_receive") if r.voucher_no == pe.name]
		self.assertEqual(len(cn_rows), 1)
		self.assertEqual(len(pe_rows), 1)

		pr.allocate_entries(to_receive=pe_rows, to_pay=cn_rows)
		self.assertFalse(any(r.is_cross_account for r in pr.allocation))
		pr.reconcile()

		# Same account + writable PE → settled directly, NO bridge JE.
		self.assertFalse(
			frappe.db.exists(
				"Journal Entry", {"company": self.company, "voucher_type": "Reconciliation Journal"}
			)
		)
		# The reference is written on the PE, pointing straight at the credit note.
		pe.reload()
		self.assertEqual(len(pe.references), 1)
		self.assertEqual(pe.references[0].reference_doctype, "Sales Invoice")
		self.assertEqual(pe.references[0].reference_name, cr_note.name)

		# Both settle.
		pr2 = self.create_payment_reconciliation()
		pr2.get_unreconciled_entries()
		remaining = {r.voucher_no for r in pr2.get("to_receive")} | {r.voucher_no for r in pr2.get("to_pay")}
		self.assertNotIn(cr_note.name, remaining)
		self.assertNotIn(pe.name, remaining)

	def test_forex_purchase_invoice_against_later_regular_payment(self):
		"""Case 2 (forex): PI in USD, paid later by a regular Payment Entry at a
		changed rate. Reconciliation must book the exact FX difference and clear
		the invoice.

		PI: 100 USD @ 50 = 5,000 INR liability.
		PE (Pay): 100 USD @ 80 = 8,000 INR.
		Settling a 5,000 INR liability with an 8,000 INR payment is a 3,000 INR
		exchange loss.
		"""
		self.supplier = "_Test Supplier USD"

		pi = self.create_purchase_invoice(qty=1, rate=100, do_not_submit=True)
		pi.supplier = self.supplier
		pi.currency = "USD"
		pi.conversion_rate = 50
		pi.credit_to = self.creditors_usd
		pi.save().submit()

		# Regular (non-advance) Payment Entry, paid later at rate 80.
		pe = self.create_payment_entry(amount=100)
		pe.payment_type = "Pay"
		pe.party_type = "Supplier"
		pe.party = self.supplier
		pe.paid_from = self.cash
		pe.paid_from_account_currency = "INR"
		pe.target_exchange_rate = 80
		pe.paid_amount = 80 * 100
		pe.received_amount = 100
		pe.paid_to = self.creditors_usd
		pe.paid_to_account_currency = "USD"
		pe.save().submit()

		pr = self.create_payment_reconciliation(party_is_customer=False)
		pr.party = self.supplier
		pr.receivable_payable_account = self.creditors_usd
		pr.get_unreconciled_entries()
		pr.allocate_entries()

		self.assertEqual(flt(pr.allocation[0].get("difference_amount")), 3000.0)
		pr.reconcile()

		# Invoice fully cleared.
		pi.reload()
		self.assertEqual(flt(pi.outstanding_amount), 0)

		# Exactly one FX loss of 3,000 INR booked against the party account.
		fx_jes = frappe.get_all(
			"Journal Entry",
			filters={
				"voucher_type": "Exchange Gain Or Loss",
				"docstatus": 1,
				"company": self.company,
			},
			pluck="name",
		)
		fx_on_party = 0.0
		for je_name in fx_jes:
			je_doc = frappe.get_doc("Journal Entry", je_name)
			for acc in je_doc.accounts:
				if acc.account == self.creditors_usd:
					fx_on_party += flt(acc.credit) - flt(acc.debit)
		self.assertEqual(fx_on_party, 3000.0)

	def test_cross_account_journal_reconciliation_bridged(self):
		"""Two JEs for one Supplier on DIFFERENT party accounts (invoice-like on
		Creditors, advance-like on the advance account). A JE split only nets its
		own account, so this is auto-bridged: a system transfer JE moves the
		balance between accounts and each side is then settled within its account.
		Previously this was refused; now it reconciles.
		"""
		self.supplier = make_supplier("_Test PR Supplier INR")

		# invoice-like JE on Creditors: Dr Expense / Cr Creditors 100 -> to_pay
		je_inv = self.create_journal_entry(self.expense_account, self.creditors, 100)
		je_inv.accounts[1].party_type = "Supplier"
		je_inv.accounts[1].party = self.supplier
		je_inv.save().submit()

		# payment-like JE on a DIFFERENT payable account -> to_receive
		je_pay = self.create_journal_entry(self.advance_payable_account, self.bank, 100)
		je_pay.accounts[0].party_type = "Supplier"
		je_pay.accounts[0].party = self.supplier
		je_pay.save().submit()

		pr = self.create_payment_reconciliation(party_is_customer=False)
		pr.party = self.supplier
		pr.receivable_payable_account = self.creditors
		pr.default_advance_account = self.advance_payable_account
		pr.get_unreconciled_entries()
		pr.allocate_entries()

		self.assertEqual(len(pr.allocation), 1)
		self.assertEqual(pr.allocation[0].is_cross_account, 1)

		pr.reconcile()

		# Both JEs fully settled → neither reappears for the party.
		pr2 = self.create_payment_reconciliation(party_is_customer=False)
		pr2.party = self.supplier
		pr2.receivable_payable_account = self.creditors
		pr2.default_advance_account = self.advance_payable_account
		pr2.get_unreconciled_entries()
		open_vouchers = {r.voucher_no for r in pr2.to_receive} | {r.voucher_no for r in pr2.to_pay}
		self.assertNotIn(je_inv.name, open_vouchers)
		self.assertNotIn(je_pay.name, open_vouchers)

	def _make_new_creditors(self):
		name = frappe.db.get_value("Account", {"account_name": "New Creditors", "company": self.company})
		if name:
			return name
		acc = frappe.new_doc("Account")
		acc.account_name = "New Creditors"
		acc.parent_account = "Accounts Payable - _PR"
		acc.company = self.company
		acc.account_currency = "INR"
		acc.account_type = "Payable"
		acc.insert()
		return acc.name

	def test_cross_account_payment_entry_non_advance(self):
		"""A plain (non-advance) PE in a NON-default party account vs an invoice in
		another account is now auto-bridged (previously refused). The PE settles
		against the transfer leg in its account; the PI against the other leg."""
		new_creditors = self._make_new_creditors()
		self.supplier = make_supplier("_Test PR Supplier INR")

		pi = self.create_purchase_invoice(qty=1, rate=100, do_not_submit=True)
		pi.supplier = self.supplier
		pi.credit_to = self.creditors
		pi.save().submit()

		pe = self.create_payment_entry(amount=100)
		pe.payment_type = "Pay"
		pe.party_type = "Supplier"
		pe.party = self.supplier
		pe.paid_from = self.bank
		pe.paid_to = new_creditors
		pe.save().submit()
		self.assertFalse(pe.book_advance_payments_in_separate_party_account)

		pr = frappe.new_doc("Payment Reconciliation")
		pr.company = self.company
		pr.party_type = "Supplier"
		pr.party = self.supplier
		pr.from_date = pr.to_date = nowdate()
		pr.get_unreconciled_entries()
		pr.allocate_entries()
		self.assertTrue(any(r.is_cross_account for r in pr.allocation))
		pr.reconcile()

		pi.reload()
		self.assertEqual(flt(pi.outstanding_amount), 0)
		self.assertEqual(pi.status, "Paid")
		# PE fully consumed.
		pr2 = frappe.new_doc("Payment Reconciliation")
		pr2.company = self.company
		pr2.party_type = "Supplier"
		pr2.party = self.supplier
		pr2.from_date = pr2.to_date = nowdate()
		pr2.get_unreconciled_entries()
		self.assertNotIn(pe.name, {r.voucher_no for r in pr2.to_receive})
		self.assertNotIn(pi.name, {r.voucher_no for r in pr2.to_pay})

	def test_cross_account_invoice_against_return(self):
		"""Invoice on one account vs a return note (DN) on another is now
		auto-bridged. Both invoices are settled as the `against` side of the
		transfer JE (the JE leg is always the voucher)."""
		new_creditors = self._make_new_creditors()
		self.supplier = make_supplier("_Test PR Supplier INR")

		pi = self.create_purchase_invoice(qty=2, rate=50, do_not_submit=True)
		pi.supplier = self.supplier
		pi.credit_to = self.creditors
		pi.save().submit()

		dn = self.create_purchase_invoice(qty=2, rate=50, do_not_submit=True)
		dn.supplier = self.supplier
		dn.credit_to = new_creditors
		dn.is_return = 1
		dn.items[0].qty = -dn.items[0].qty
		dn.save().submit()

		pr = frappe.new_doc("Payment Reconciliation")
		pr.company = self.company
		pr.party_type = "Supplier"
		pr.party = self.supplier
		pr.from_date = pr.to_date = nowdate()
		pr.get_unreconciled_entries()
		pr.allocate_entries()
		self.assertTrue(any(r.is_cross_account for r in pr.allocation))
		pr.reconcile()

		pi.reload()
		self.assertEqual(flt(pi.outstanding_amount), 0)
		dn.reload()
		self.assertEqual(flt(dn.outstanding_amount), 0)

	def _reconcile_forex_pi_against_regular_payment(self, pi_rate, pe_rate, pe_usd, expected_diff):
		"""Shared driver: USD PI settled later by a regular USD Pay PE at a
		different rate. Asserts both the allocation `difference_amount` and the
		net FX posted to the party account equal `expected_diff` (sign: +loss,
		-gain on the Creditors leg)."""
		self.supplier = "_Test Supplier USD"
		pi = self.create_purchase_invoice(qty=1, rate=100, do_not_submit=True)
		pi.supplier = self.supplier
		pi.currency = "USD"
		pi.conversion_rate = pi_rate
		pi.credit_to = self.creditors_usd
		pi.save().submit()

		pe = self.create_payment_entry(amount=pe_usd)
		pe.payment_type = "Pay"
		pe.party_type = "Supplier"
		pe.party = self.supplier
		pe.paid_from = self.cash
		pe.paid_from_account_currency = "INR"
		pe.target_exchange_rate = pe_rate
		pe.paid_amount = pe_rate * pe_usd
		pe.received_amount = pe_usd
		pe.paid_to = self.creditors_usd
		pe.paid_to_account_currency = "USD"
		pe.save().submit()

		pr = self.create_payment_reconciliation(party_is_customer=False)
		pr.party = self.supplier
		pr.receivable_payable_account = self.creditors_usd
		pr.get_unreconciled_entries()
		pr.allocate_entries()
		self.assertEqual(flt(pr.allocation[0].get("difference_amount")), flt(expected_diff))
		pr.reconcile()

		fx = 0.0
		for n in frappe.get_all(
			"Journal Entry",
			filters={"voucher_type": "Exchange Gain Or Loss", "docstatus": 1, "company": self.company},
			pluck="name",
		):
			for a in frappe.get_doc("Journal Entry", n).accounts:
				if a.account == self.creditors_usd:
					fx += flt(a.credit) - flt(a.debit)
		self.assertEqual(fx, flt(expected_diff))

	def test_forex_purchase_invoice_gain_on_later_payment(self):
		# PI 100 USD @ 80 (8,000 INR), paid 100 USD @ 50 (5,000 INR) -> 3,000 gain.
		self._reconcile_forex_pi_against_regular_payment(
			pi_rate=80, pe_rate=50, pe_usd=100, expected_diff=-3000
		)

	def test_forex_purchase_invoice_partial_later_payment(self):
		# PI 100 USD @ 50, partial 60 USD @ 80 -> loss on 60 = 60*(80-50) = 1,800.
		self._reconcile_forex_pi_against_regular_payment(
			pi_rate=50, pe_rate=80, pe_usd=60, expected_diff=1800
		)

	def test_forex_pi_reconcile_cancel_propagates_to_fx_je(self):
		"""Reconciling a USD PI against a later USD payment books an FX JE;
		cancelling the invoice must cancel that FX JE too (no orphaned gain/loss).
		"""
		self.supplier = "_Test Supplier USD"
		pi = self.create_purchase_invoice(qty=1, rate=10, do_not_submit=True)
		pi.supplier = self.supplier
		pi.currency = "USD"
		pi.conversion_rate = 50
		pi.credit_to = self.creditors_usd
		pi.save().submit()

		pe = self.create_payment_entry(amount=10)
		pe.payment_type = "Pay"
		pe.party_type = "Supplier"
		pe.party = self.supplier
		pe.paid_from = self.cash
		pe.paid_from_account_currency = "INR"
		pe.target_exchange_rate = 80
		pe.paid_amount = 800
		pe.received_amount = 10
		pe.paid_to = self.creditors_usd
		pe.paid_to_account_currency = "USD"
		pe.save().submit()

		pr = self.create_payment_reconciliation(party_is_customer=False)
		pr.party = self.supplier
		pr.receivable_payable_account = self.creditors_usd
		pr.get_unreconciled_entries()
		pr.allocate_entries()
		pr.reconcile()

		fx_jes = frappe.get_all(
			"Journal Entry",
			filters={
				"voucher_type": "Exchange Gain Or Loss",
				"docstatus": 1,
				"company": self.company,
			},
			pluck="name",
		)
		self.assertEqual(len(fx_jes), 1)
		fx_je = frappe.get_doc("Journal Entry", fx_jes[0])
		party_leg = sum(
			flt(a.credit) - flt(a.debit) for a in fx_je.accounts if a.account == self.creditors_usd
		)
		self.assertEqual(party_leg, 300.0)

		# Cancelling the invoice must cancel the linked FX JE.
		pi.reload()
		pi.cancel()
		fx_je.reload()
		self.assertEqual(fx_je.docstatus, 2)

	def test_partial_opposite_leg_journal_split(self):
		"""Opposite-leg JE voucher (Cr Creditors 1000) settled only PARTIALLY
		(400) must leave 600 on the leg, not double or zero it."""
		self.supplier = make_supplier("_Test PR Supplier INR")

		je_cr = self.create_journal_entry(self.expense_account, self.creditors, 1000)
		je_cr.accounts[1].party_type = "Supplier"
		je_cr.accounts[1].party = self.supplier
		je_cr.save().submit()

		je_dr = self.create_journal_entry(self.creditors, self.bank, 400)
		je_dr.accounts[0].party_type = "Supplier"
		je_dr.accounts[0].party = self.supplier
		je_dr.save().submit()

		pr = self.create_payment_reconciliation(party_is_customer=False)
		pr.party = self.supplier
		pr.receivable_payable_account = self.creditors
		pr.get_unreconciled_entries()
		pr.allocate_entries()
		pr.reconcile()

		pay = {r.voucher_no: flt(r.outstanding_amount) for r in pr.get("to_pay")}
		self.assertEqual(pay.get(je_cr.name), 600)
		self.assertNotIn(je_dr.name, {r.voucher_no for r in pr.get("to_receive")})

	def test_opposite_leg_journal_split_across_two_allocations(self):
		"""One opposite-leg JE voucher (Cr Creditors 1000) split across TWO
		against entries (400 + 600) in a single reconcile. Exercises the
		multi-allocation-per-JEA path together with the opposite-leg fix; the
		old negate-then-double logic corrupted this badly."""
		self.supplier = make_supplier("_Test PR Supplier INR")

		je_cr = self.create_journal_entry(self.expense_account, self.creditors, 1000)
		je_cr.accounts[1].party_type = "Supplier"
		je_cr.accounts[1].party = self.supplier
		je_cr.save().submit()

		je_dr1 = self.create_journal_entry(self.creditors, self.bank, 400)
		je_dr1.accounts[0].party_type = "Supplier"
		je_dr1.accounts[0].party = self.supplier
		je_dr1.save().submit()

		je_dr2 = self.create_journal_entry(self.creditors, self.bank, 600)
		je_dr2.accounts[0].party_type = "Supplier"
		je_dr2.accounts[0].party = self.supplier
		je_dr2.save().submit()

		pr = self.create_payment_reconciliation(party_is_customer=False)
		pr.party = self.supplier
		pr.receivable_payable_account = self.creditors
		pr.get_unreconciled_entries()
		pr.allocate_entries()
		pr.reconcile()

		remaining = {r.voucher_no for r in pr.get("to_receive")} | {r.voucher_no for r in pr.get("to_pay")}
		self.assertNotIn(je_cr.name, remaining)
		self.assertNotIn(je_dr1.name, remaining)
		self.assertNotIn(je_dr2.name, remaining)

	def _make_pay_pe_usd(self, usd, rate):
		pe = self.create_payment_entry(amount=usd)
		pe.payment_type = "Pay"
		pe.party_type = "Supplier"
		pe.party = self.supplier
		pe.paid_from = self.cash
		pe.paid_from_account_currency = "INR"
		pe.target_exchange_rate = rate
		pe.paid_amount = rate * usd
		pe.received_amount = usd
		pe.paid_to = self.creditors_usd
		pe.paid_to_account_currency = "USD"
		return pe.save().submit()

	def _make_pi_usd(self, usd, rate):
		pi = self.create_purchase_invoice(qty=1, rate=usd, do_not_submit=True)
		pi.supplier = self.supplier
		pi.currency = "USD"
		pi.conversion_rate = rate
		pi.credit_to = self.creditors_usd
		return pi.save().submit()

	def _total_fx_on_creditors_usd(self):
		total = 0.0
		for n in frappe.get_all(
			"Journal Entry",
			filters={"voucher_type": "Exchange Gain Or Loss", "docstatus": 1, "company": self.company},
			pluck="name",
		):
			for a in frappe.get_doc("Journal Entry", n).accounts:
				if a.account == self.creditors_usd:
					total += flt(a.credit) - flt(a.debit)
		return total

	def test_forex_two_payments_one_invoice(self):
		"""Two USD payments (each 50 @ 80) settle one USD PI (100 @ 50). Total FX
		booked must equal 100 * (80-50) = 3000, invoice cleared."""
		self.supplier = "_Test Supplier USD"
		pi = self._make_pi_usd(100, 50)
		self._make_pay_pe_usd(50, 80)
		self._make_pay_pe_usd(50, 80)

		pr = self.create_payment_reconciliation(party_is_customer=False)
		pr.party = self.supplier
		pr.receivable_payable_account = self.creditors_usd
		pr.get_unreconciled_entries()
		pr.allocate_entries()
		pr.reconcile()

		pi.reload()
		self.assertEqual(flt(pi.outstanding_amount), 0)
		self.assertEqual(self._total_fx_on_creditors_usd(), 3000.0)

	def test_forex_one_payment_two_invoices(self):
		"""One USD payment (100 @ 80) settles two USD PIs (60 @ 50, 40 @ 50).
		Two references on one PE -> total FX must equal 100*(80-50)=3000 with no
		dedupe collision dropping/doubling a leg."""
		self.supplier = "_Test Supplier USD"
		pi1 = self._make_pi_usd(60, 50)
		pi2 = self._make_pi_usd(40, 50)
		self._make_pay_pe_usd(100, 80)

		pr = self.create_payment_reconciliation(party_is_customer=False)
		pr.party = self.supplier
		pr.receivable_payable_account = self.creditors_usd
		pr.get_unreconciled_entries()
		pr.allocate_entries()
		pr.reconcile()

		pi1.reload()
		pi2.reload()
		self.assertEqual(flt(pi1.outstanding_amount), 0)
		self.assertEqual(flt(pi2.outstanding_amount), 0)
		self.assertEqual(self._total_fx_on_creditors_usd(), 3000.0)

	def test_forex_pi_reconcile_cancel_payment_propagates_to_fx_je(self):
		"""Symmetric to the PI-cancel test: cancelling the PAYMENT must also
		cancel the linked FX JE."""
		self.supplier = "_Test Supplier USD"
		self._make_pi_usd(10, 50)
		pe = self._make_pay_pe_usd(10, 80)

		pr = self.create_payment_reconciliation(party_is_customer=False)
		pr.party = self.supplier
		pr.receivable_payable_account = self.creditors_usd
		pr.get_unreconciled_entries()
		pr.allocate_entries()
		pr.reconcile()

		fx_jes = frappe.get_all(
			"Journal Entry",
			filters={"voucher_type": "Exchange Gain Or Loss", "docstatus": 1, "company": self.company},
			pluck="name",
		)
		self.assertEqual(len(fx_jes), 1)
		fx_je = frappe.get_doc("Journal Entry", fx_jes[0])

		pe.reload()
		pe.cancel()
		fx_je.reload()
		self.assertEqual(fx_je.docstatus, 2)

	def test_debit_leg_journal_voucher_against_invoice(self):
		"""JE voucher whose party leg is a DEBIT (Dr Creditors) reconciled
		against a PI. Exercises the to_receive => debit `dr_or_cr` branch with a
		real settlement (mirror of the Cr-leg cases)."""
		self.supplier = make_supplier("_Test PR Supplier INR")

		pi = self.create_purchase_invoice(qty=1, rate=1000, do_not_submit=True)
		pi.supplier = self.supplier
		pi.credit_to = self.creditors
		pi.save().submit()

		je = self.create_journal_entry(self.creditors, self.bank, 1000)  # Dr Creditors 1000
		je.accounts[0].party_type = "Supplier"
		je.accounts[0].party = self.supplier
		je.save().submit()

		pr = self.create_payment_reconciliation(party_is_customer=False)
		pr.party = self.supplier
		pr.receivable_payable_account = self.creditors
		pr.get_unreconciled_entries()
		pr.allocate_entries()
		pr.reconcile()

		remaining = {r.voucher_no for r in pr.get("to_receive")} | {r.voucher_no for r in pr.get("to_pay")}
		self.assertNotIn(pi.name, remaining)
		self.assertNotIn(je.name, remaining)

	def test_same_account_both_legs_journal_split_isolation(self):
		"""A JE with BOTH a Dr and a Cr party leg on the SAME account. Settling
		the Cr leg against an external payment must split ONLY that leg and leave
		the Dr leg fully open (the splitter targets one JEA by name)."""
		self.supplier = make_supplier("_Test PR Supplier INR")

		je = frappe.new_doc("Journal Entry")
		je.posting_date = nowdate()
		je.company = self.company
		je.user_remark = "both party legs on one account"
		je.append(
			"accounts",
			{
				"account": self.creditors,
				"party_type": "Supplier",
				"party": self.supplier,
				"debit_in_account_currency": 1000,
				"cost_center": self.cost_center,
			},
		)
		je.append(
			"accounts",
			{
				"account": self.creditors,
				"party_type": "Supplier",
				"party": self.supplier,
				"credit_in_account_currency": 600,
				"cost_center": self.cost_center,
			},
		)
		je.append(
			"accounts",
			{
				"account": self.expense_account,
				"debit_in_account_currency": 600,
				"credit_in_account_currency": 0,
				"cost_center": self.cost_center,
			},
		)
		# balance: Dr 1000 + Dr 600(expense) = 1600 ; Cr 600 -> need Cr 1000 more
		je.append(
			"accounts",
			{
				"account": self.bank,
				"credit_in_account_currency": 1000,
				"cost_center": self.cost_center,
			},
		)
		je.save().submit()

		je_pay = self.create_journal_entry(self.creditors, self.bank, 600)  # Dr Creditors 600
		je_pay.accounts[0].party_type = "Supplier"
		je_pay.accounts[0].party = self.supplier
		je_pay.save().submit()

		pr = self.create_payment_reconciliation(party_is_customer=False)
		pr.party = self.supplier
		pr.receivable_payable_account = self.creditors
		pr.get_unreconciled_entries()

		cr_leg = [r.as_dict() for r in pr.get("to_pay") if r.voucher_no == je.name]
		pay_row = [r.as_dict() for r in pr.get("to_receive") if r.voucher_no == je_pay.name]
		self.assertEqual(len(cr_leg), 1)
		self.assertEqual(len(pay_row), 1)

		pr.allocate_entries(to_receive=pay_row, to_pay=cr_leg)
		pr.reconcile()

		recv = {r.voucher_no: flt(r.outstanding_amount) for r in pr.get("to_receive")}
		pay_nos = {r.voucher_no for r in pr.get("to_pay")}
		self.assertEqual(recv.get(je.name), 1000)  # Dr leg untouched
		self.assertNotIn(je.name, pay_nos)  # Cr leg settled
		self.assertNotIn(je_pay.name, recv)  # external payment settled

	# ------------------------------------------------------------------ #
	# Cross-account auto-bridge (transfer-JE + split) — cases where the two
	# reconciled vouchers sit in DIFFERENT party accounts of the same party.
	# ------------------------------------------------------------------ #
	def _make_account(self, account_name, parent, account_type, currency="INR"):
		existing = frappe.db.get_value(
			"Account", {"account_name": account_name, "company": self.company}, "name"
		)
		if existing:
			return existing
		acc = frappe.new_doc("Account")
		acc.account_name = account_name
		acc.parent_account = parent
		acc.company = self.company
		acc.account_currency = currency
		acc.account_type = account_type
		acc.insert()
		return acc.name

	def _cross_account_pr(self, party, party_is_customer):
		"""PR with NO account filter so the fetcher pulls every party account."""
		pr = frappe.new_doc("Payment Reconciliation")
		pr.company = self.company
		pr.party_type = "Customer" if party_is_customer else "Supplier"
		pr.party = party
		pr.from_date = add_days(nowdate(), -5)
		pr.to_date = add_days(nowdate(), 5)
		return pr

	def test_cross_account_pi_against_advance_je_supplier(self):
		"""Case 3 (the reported scenario): PI in a second Liability account, advance
		paid via JE (Dr) in the default Creditors. The advance leg is in the
		UNNATURAL direction of its account, so it must be the voucher that gets
		split. After reconcile both clear and a single system transfer JE exists.
		"""
		self.supplier = make_supplier("_Test Supplier")
		creditors2 = self._make_account("Creditors New", "Accounts Payable - _PR", "Payable")
		amount = 1000

		pi = make_purchase_invoice(
			company=self.company,
			supplier=self.supplier,
			cost_center=self.cost_center,
			warehouse=self.warehouse,
			expense_account=self.expense_account,
			qty=1,
			rate=amount,
			item_code=self.item,
			do_not_save=True,
		)
		pi.credit_to = creditors2
		pi.save().submit()

		# Advance paid: Dr Creditors (default), Cr Bank — a debit in a Liability acct.
		adv = self.create_journal_entry(self.creditors, self.bank, amount)
		adv.accounts[0].party_type = "Supplier"
		adv.accounts[0].party = self.supplier
		adv.save().submit()

		pr = self._cross_account_pr(self.supplier, party_is_customer=False)
		pr.get_unreconciled_entries()

		recv = [r for r in pr.to_receive if r.voucher_no == adv.name]
		pay = [r for r in pr.to_pay if r.voucher_no == pi.name]
		self.assertEqual(len(recv), 1, "advance JE (Dr Creditors) → to_receive")
		self.assertEqual(len(pay), 1, "PI (Cr Creditors New) → to_pay")
		self.assertNotEqual(recv[0].account, pay[0].account)

		pr.allocate_entries(to_receive=[r.as_dict() for r in recv], to_pay=[r.as_dict() for r in pay])
		self.assertEqual(len(pr.allocation), 1)
		self.assertEqual(pr.allocation[0].is_cross_account, 1)

		pr.reconcile()

		pi.reload()
		self.assertEqual(pi.outstanding_amount, 0)
		self.assertEqual(pi.status, "Paid")

		# A system transfer JE between the two accounts was posted.
		transfer = frappe.db.get_all(
			"Journal Entry",
			filters={"company": self.company, "is_system_generated": 1, "name": ("!=", adv.name)},
			pluck="name",
		)
		self.assertTrue(transfer, "expected a system-generated transfer JE")

		# Nothing left open for the party.
		pr2 = self._cross_account_pr(self.supplier, party_is_customer=False)
		pr2.get_unreconciled_entries()
		self.assertNotIn(pi.name, {r.voucher_no for r in pr2.to_pay})
		self.assertNotIn(adv.name, {r.voucher_no for r in pr2.to_receive})

	def test_cross_account_si_against_advance_je_customer(self):
		"""Case 4: SI in a second Receivable account, advance received via JE (Cr)
		in the default Debtors. Both accounts are Asset; the SI is natural (Dr) and
		the advance is the credit-side that the transfer leg references."""
		debtors2 = self._make_account("Debtors New", "Accounts Receivable - _PR", "Receivable")
		amount = 800

		si = create_sales_invoice(
			company=self.company,
			customer=self.customer,
			debit_to=debtors2,
			cost_center=self.cost_center,
			warehouse=self.warehouse,
			income_account=self.income_account,
			expense_account=self.expense_account,
			qty=1,
			rate=amount,
			currency="INR",
		)

		# Advance received: Cr Debtors (default), Dr Bank.
		adv = self.create_journal_entry(self.bank, self.debit_to, amount)
		adv.accounts[1].party_type = "Customer"
		adv.accounts[1].party = self.customer
		adv.save().submit()

		pr = self._cross_account_pr(self.customer, party_is_customer=True)
		pr.get_unreconciled_entries()

		recv = [r for r in pr.to_receive if r.voucher_no == si.name]
		pay = [r for r in pr.to_pay if r.voucher_no == adv.name]
		self.assertEqual(len(recv), 1, "SI (Dr Debtors New) → to_receive")
		self.assertEqual(len(pay), 1, "advance JE (Cr Debtors) → to_pay")
		self.assertNotEqual(recv[0].account, pay[0].account)

		pr.allocate_entries(to_receive=[r.as_dict() for r in recv], to_pay=[r.as_dict() for r in pay])
		pr.reconcile()

		si.reload()
		self.assertEqual(si.outstanding_amount, 0)
		self.assertEqual(si.status, "Paid")

	def test_cross_account_je_against_je_supplier(self):
		"""Case 7: two JEs for one supplier in two different Liability accounts."""
		self.supplier = make_supplier("_Test Supplier")
		creditors2 = self._make_account("Creditors New", "Accounts Payable - _PR", "Payable")
		amount = 500

		# Invoice-like: Cr Creditors New (we owe).
		inv_je = self.create_journal_entry(self.expense_account, creditors2, amount)
		inv_je.accounts[1].party_type = "Supplier"
		inv_je.accounts[1].party = self.supplier
		inv_je.save().submit()

		# Payment-like: Dr Creditors (advance paid).
		pay_je = self.create_journal_entry(self.creditors, self.bank, amount)
		pay_je.accounts[0].party_type = "Supplier"
		pay_je.accounts[0].party = self.supplier
		pay_je.save().submit()

		pr = self._cross_account_pr(self.supplier, party_is_customer=False)
		pr.get_unreconciled_entries()

		recv = [r for r in pr.to_receive if r.voucher_no == pay_je.name]
		pay = [r for r in pr.to_pay if r.voucher_no == inv_je.name]
		self.assertEqual(len(recv), 1)
		self.assertEqual(len(pay), 1)

		pr.allocate_entries(to_receive=[r.as_dict() for r in recv], to_pay=[r.as_dict() for r in pay])
		pr.reconcile()

		# Both JEs fully settled → neither reappears.
		pr2 = self._cross_account_pr(self.supplier, party_is_customer=False)
		pr2.get_unreconciled_entries()
		self.assertNotIn(inv_je.name, {r.voucher_no for r in pr2.to_pay})
		self.assertNotIn(pay_je.name, {r.voucher_no for r in pr2.to_receive})

	def test_cross_account_partial_leaves_remainder(self):
		"""Partial cross-account allocation: the larger side keeps its remainder."""
		self.supplier = make_supplier("_Test Supplier")
		creditors2 = self._make_account("Creditors New", "Accounts Payable - _PR", "Payable")

		pi = make_purchase_invoice(
			company=self.company,
			supplier=self.supplier,
			cost_center=self.cost_center,
			warehouse=self.warehouse,
			expense_account=self.expense_account,
			qty=1,
			rate=1000,
			item_code=self.item,
			do_not_save=True,
		)
		pi.credit_to = creditors2
		pi.save().submit()
		# Advance only covers 400 of the 1000 PI.
		adv = self.create_journal_entry(self.creditors, self.bank, 400)
		adv.accounts[0].party_type = "Supplier"
		adv.accounts[0].party = self.supplier
		adv.save().submit()

		pr = self._cross_account_pr(self.supplier, party_is_customer=False)
		pr.get_unreconciled_entries()
		recv = [r for r in pr.to_receive if r.voucher_no == adv.name]
		pay = [r for r in pr.to_pay if r.voucher_no == pi.name]
		pr.allocate_entries(to_receive=[r.as_dict() for r in recv], to_pay=[r.as_dict() for r in pay])
		pr.reconcile()

		pi.reload()
		self.assertEqual(pi.outstanding_amount, 600)
		# Advance fully consumed.
		pr2 = self._cross_account_pr(self.supplier, party_is_customer=False)
		pr2.get_unreconciled_entries()
		self.assertNotIn(adv.name, {r.voucher_no for r in pr2.to_receive})

	def test_cross_account_routing(self):
		"""`_needs_bridge`: bridge every cross-account pair EXCEPT a PE that books its
		advance in a separate party account (handled natively). For a same-account
		pair, bridge only when neither side is writable (invoice↔note); a writable
		side (PE/JE) is mutated in place, no bridge."""
		from erpnext.accounts.doctype.payment_reconciliation.payment_reconciliation import (
			ReconcileRouter,
		)

		self.supplier = make_supplier("_Test Supplier")
		new_creditors = self._make_new_creditors()
		pr = self.create_payment_reconciliation(party_is_customer=False)
		router = ReconcileRouter(pr)

		# Plain (non-advance) PE in a non-default account → bridged.
		plain_pe = self.create_payment_entry(amount=100)
		plain_pe.payment_type = "Pay"
		plain_pe.party_type = "Supplier"
		plain_pe.party = self.supplier
		plain_pe.paid_from = self.bank
		plain_pe.paid_to = self.creditors
		plain_pe.save().submit()
		self.assertFalse(plain_pe.book_advance_payments_in_separate_party_account)
		plain_pe_row = frappe._dict(
			to_receive_voucher_type="Payment Entry",
			to_receive_voucher_no=plain_pe.name,
			to_receive_account=self.creditors,
			to_pay_voucher_type="Purchase Invoice",
			to_pay_voucher_no="PI-1",
			to_pay_account=new_creditors,
		)
		self.assertTrue(router._needs_bridge(plain_pe_row))

		# PE booking advance in a separate party account → NOT bridged (native path).
		frappe.db.set_value(
			"Company",
			self.company,
			{
				"book_advance_payments_in_separate_party_account": 1,
				"default_advance_paid_account": self.advance_payable_account,
			},
		)
		adv_pe = self.create_payment_entry(amount=100)
		adv_pe.payment_type = "Pay"
		adv_pe.party_type = "Supplier"
		adv_pe.party = self.supplier
		adv_pe.paid_from = self.bank
		adv_pe.paid_to = self.advance_payable_account
		adv_pe.save().submit()
		self.assertTrue(adv_pe.book_advance_payments_in_separate_party_account)
		adv_pe_row = frappe._dict(
			to_receive_voucher_type="Payment Entry",
			to_receive_voucher_no=adv_pe.name,
			to_receive_account=self.advance_payable_account,
			to_pay_voucher_type="Purchase Invoice",
			to_pay_voucher_no="PI-1",
			to_pay_account=self.creditors,
		)
		self.assertFalse(router._needs_bridge(adv_pe_row))

		# JE cross-account → bridged.
		je_row = frappe._dict(
			to_receive_voucher_type="Journal Entry",
			to_receive_voucher_no="JE-1",
			to_receive_account=self.creditors,
			to_pay_voucher_type="Purchase Invoice",
			to_pay_voucher_no="PI-1",
			to_pay_account=new_creditors,
		)
		self.assertTrue(router._needs_bridge(je_row))

		# Same account, a writable side (JE) → mutated in place, NOT bridged.
		same_acct = frappe._dict(
			to_receive_voucher_type="Journal Entry",
			to_receive_voucher_no="JE-1",
			to_receive_account=self.creditors,
			to_pay_voucher_type="Purchase Invoice",
			to_pay_voucher_no="PI-1",
			to_pay_account=self.creditors,
		)
		self.assertFalse(router._needs_bridge(same_acct))

		# Same account, NEITHER side writable (invoice↔return note) → bridged
		# (replaces the old `reconcile_dr_cr_note` path).
		same_acct_note = frappe._dict(
			to_receive_voucher_type="Purchase Invoice",
			to_receive_voucher_no="PI-1",
			to_receive_account=self.creditors,
			to_pay_voucher_type="Purchase Invoice",
			to_pay_voucher_no="PI-2",
			to_pay_account=self.creditors,
		)
		self.assertTrue(router._needs_bridge(same_acct_note))

	def _reconcile_pi_pe_cross_account(self):
		"""PI (default Creditors) + non-advance PE (New Creditors) reconciled via the
		bridge. Returns (pi, pe, bridge_name)."""
		new_creditors = self._make_new_creditors()
		self.supplier = make_supplier("_Test PR Supplier INR")

		pi = self.create_purchase_invoice(qty=1, rate=100, do_not_submit=True)
		pi.supplier = self.supplier
		pi.credit_to = self.creditors
		pi.save().submit()

		pe = self.create_payment_entry(amount=100)
		pe.payment_type = "Pay"
		pe.party_type = "Supplier"
		pe.party = self.supplier
		pe.paid_from = self.bank
		pe.paid_to = new_creditors
		pe.save().submit()

		pr = frappe.new_doc("Payment Reconciliation")
		pr.company = self.company
		pr.party_type = "Supplier"
		pr.party = self.supplier
		pr.from_date = pr.to_date = nowdate()
		pr.get_unreconciled_entries()
		pr.allocate_entries()
		pr.reconcile()

		pi.reload()
		self.assertEqual(flt(pi.outstanding_amount), 0)
		return pi, pe, self._bridge_je_name()

	def test_cross_account_unreconcile_payment_entry_bridge(self):
		"""Unreconciling a PE-bridged cross-account reconcile cancels the bridge and
		restores BOTH the PI and the PE (the PE references the bridge via a Payment
		Entry Reference, which the unwind must also unlink)."""
		import json

		from erpnext.accounts.doctype.unreconcile_payment.unreconcile_payment import (
			create_unreconcile_doc_for_selection,
		)

		pi, pe, bridge = self._reconcile_pi_pe_cross_account()
		self.assertTrue(bridge)
		pe.reload()
		self.assertEqual(flt(pe.unallocated_amount), 0)

		create_unreconcile_doc_for_selection(
			json.dumps(
				[
					{
						"company": self.company,
						"voucher_type": "Journal Entry",
						"voucher_no": bridge,
						"against_voucher_type": "Purchase Invoice",
						"against_voucher_no": pi.name,
					}
				]
			)
		)

		self.assertEqual(frappe.db.get_value("Journal Entry", bridge, "docstatus"), 2)
		pi.reload()
		self.assertEqual(flt(pi.outstanding_amount), 100)
		pe.reload()
		self.assertEqual(flt(pe.unallocated_amount), 100)

	def test_cross_account_cancel_invoice_cancels_pe_bridge(self):
		"""Cancelling the PI of a PE-bridged reconcile cancels the bridge and frees
		the PE."""
		frappe.db.set_single_value("Accounts Settings", "unlink_payment_on_cancellation_of_invoice", 1)
		pi, pe, bridge = self._reconcile_pi_pe_cross_account()

		pi.reload()
		pi.cancel()

		self.assertEqual(frappe.db.get_value("Journal Entry", bridge, "docstatus"), 2)
		pe.reload()
		self.assertEqual(flt(pe.unallocated_amount), 100)

	def test_cross_account_unreconcile_return_bridge(self):
		"""Unreconciling an invoice↔return-note cross-account reconcile cancels the
		bridge and reopens both invoices."""
		import json

		from erpnext.accounts.doctype.unreconcile_payment.unreconcile_payment import (
			create_unreconcile_doc_for_selection,
		)

		new_creditors = self._make_new_creditors()
		self.supplier = make_supplier("_Test PR Supplier INR")

		pi = self.create_purchase_invoice(qty=2, rate=50, do_not_submit=True)
		pi.supplier = self.supplier
		pi.credit_to = self.creditors
		pi.save().submit()

		dn = self.create_purchase_invoice(qty=2, rate=50, do_not_submit=True)
		dn.supplier = self.supplier
		dn.credit_to = new_creditors
		dn.is_return = 1
		dn.items[0].qty = -dn.items[0].qty
		dn.save().submit()

		pr = frappe.new_doc("Payment Reconciliation")
		pr.company = self.company
		pr.party_type = "Supplier"
		pr.party = self.supplier
		pr.from_date = pr.to_date = nowdate()
		pr.get_unreconciled_entries()
		pr.allocate_entries()
		pr.reconcile()

		pi.reload()
		self.assertEqual(flt(pi.outstanding_amount), 0)
		bridge = self._bridge_je_name()

		create_unreconcile_doc_for_selection(
			json.dumps(
				[
					{
						"company": self.company,
						"voucher_type": "Journal Entry",
						"voucher_no": bridge,
						"against_voucher_type": "Purchase Invoice",
						"against_voucher_no": pi.name,
					}
				]
			)
		)

		self.assertEqual(frappe.db.get_value("Journal Entry", bridge, "docstatus"), 2)
		pi.reload()
		self.assertEqual(flt(pi.outstanding_amount), 100)

	def test_allocator_prefers_same_account(self):
		"""When a receivable can pair with EITHER a same-account or an (older)
		cross-account payable, the Allocator drains the same-account one first —
		so no needless cross-account bridge JE is minted."""
		from erpnext.accounts.doctype.payment_reconciliation.payment_reconciliation import (
			Allocator,
		)

		pr = self.create_payment_reconciliation()

		def _row(no, amount, account, days_old):
			return frappe._dict(
				{
					"voucher_type": "Journal Entry",
					"voucher_no": no,
					"voucher_row": no + "-r",
					"outstanding_amount": amount,
					"amount": amount,
					"currency": "INR",
					"exchange_rate": 1,
					"posting_date": add_days(nowdate(), -days_old),
					"account": account,
					"party_type": "Customer",
					"party": self.customer,
					"is_advance": 0,
				}
			)

		recv = [_row("R1", 100, self.debit_to, 0)]
		# P_cross is older (would win pure FIFO); P_same shares the receivable's account.
		pay = [
			_row("P_cross", 100, self.advance_receivable_account, 5),
			_row("P_same", 100, self.debit_to, 0),
		]

		allocations = Allocator(pr, to_receive=recv, to_pay=pay).allocate()

		self.assertEqual(len(allocations), 1)
		self.assertEqual(allocations[0]["to_pay_voucher_no"], "P_same")
		self.assertEqual(allocations[0]["is_cross_account"], 0)

	def test_allocator_falls_back_to_cross_account(self):
		"""With no same-account match, the Allocator still pairs cross-account
		(flagging it) so the bridge can settle it."""
		from erpnext.accounts.doctype.payment_reconciliation.payment_reconciliation import (
			Allocator,
		)

		pr = self.create_payment_reconciliation()

		def _row(no, amount, account):
			return frappe._dict(
				{
					"voucher_type": "Journal Entry",
					"voucher_no": no,
					"voucher_row": no + "-r",
					"outstanding_amount": amount,
					"amount": amount,
					"currency": "INR",
					"exchange_rate": 1,
					"posting_date": nowdate(),
					"account": account,
					"party_type": "Customer",
					"party": self.customer,
					"is_advance": 0,
				}
			)

		recv = [_row("R1", 100, self.debit_to)]
		pay = [_row("P1", 100, self.advance_receivable_account)]

		allocations = Allocator(pr, to_receive=recv, to_pay=pay).allocate()
		self.assertEqual(len(allocations), 1)
		self.assertEqual(allocations[0]["is_cross_account"], 1)

	def test_cross_account_negative_value_je_supplier(self):
		"""Cross-account bridge with the advance booked as a NEGATIVE credit on
		Creditors (≡ a debit) instead of a positive debit. Should classify and
		bridge identically to the positive-debit advance (case 3)."""
		self.supplier = make_supplier("_Test Supplier")
		creditors2 = self._make_account("Creditors New", "Accounts Payable - _PR", "Payable")
		amount = 1000

		pi = make_purchase_invoice(
			company=self.company,
			supplier=self.supplier,
			cost_center=self.cost_center,
			warehouse=self.warehouse,
			expense_account=self.expense_account,
			qty=1,
			rate=amount,
			item_code=self.item,
			do_not_save=True,
		)
		pi.credit_to = creditors2
		pi.save().submit()

		# Advance booked as a negative credit on Creditors (≡ Dr Creditors 1000).
		adv = self.create_journal_entry(self.creditors, self.bank, amount)
		adv.accounts[0].party_type = "Supplier"
		adv.accounts[0].party = self.supplier
		adv.accounts[0].debit_in_account_currency = 0
		adv.accounts[0].credit_in_account_currency = -amount
		adv.save().submit()

		pr = self._cross_account_pr(self.supplier, party_is_customer=False)
		pr.get_unreconciled_entries()
		recv = [r for r in pr.to_receive if r.voucher_no == adv.name]
		pay = [r for r in pr.to_pay if r.voucher_no == pi.name]
		self.assertEqual(len(recv), 1, "negative-credit advance → to_receive")
		self.assertEqual(len(pay), 1)
		self.assertNotEqual(recv[0].account, pay[0].account)

		pr.allocate_entries(to_receive=[r.as_dict() for r in recv], to_pay=[r.as_dict() for r in pay])
		pr.reconcile()

		pi.reload()
		self.assertEqual(pi.outstanding_amount, 0)
		self.assertEqual(pi.status, "Paid")

		pr2 = self._cross_account_pr(self.supplier, party_is_customer=False)
		pr2.get_unreconciled_entries()
		self.assertNotIn(pi.name, {r.voucher_no for r in pr2.to_pay})
		self.assertNotIn(adv.name, {r.voucher_no for r in pr2.to_receive})

	def _assert_cross_account_settled(self, party, party_is_customer, recv_no, pay_no, invoice=None):
		"""Allocate the (recv_no, pay_no) cross-account pair, reconcile, and assert
		both vouchers fully settle (don't reappear)."""
		pr = self._cross_account_pr(party, party_is_customer)
		pr.get_unreconciled_entries()
		recv = [r for r in pr.to_receive if r.voucher_no == recv_no]
		pay = [r for r in pr.to_pay if r.voucher_no == pay_no]
		self.assertEqual(len(recv), 1, f"{recv_no} expected in to_receive")
		self.assertEqual(len(pay), 1, f"{pay_no} expected in to_pay")
		self.assertNotEqual(recv[0].account, pay[0].account, "should be cross-account")

		pr.allocate_entries(to_receive=[r.as_dict() for r in recv], to_pay=[r.as_dict() for r in pay])
		self.assertEqual(pr.allocation[0].is_cross_account, 1)
		pr.reconcile()

		if invoice is not None:
			invoice.reload()
			self.assertEqual(invoice.outstanding_amount, 0)

		pr2 = self._cross_account_pr(party, party_is_customer)
		pr2.get_unreconciled_entries()
		self.assertNotIn(recv_no, {r.voucher_no for r in pr2.to_receive})
		self.assertNotIn(pay_no, {r.voucher_no for r in pr2.to_pay})

	def test_cross_account_mixed_root_supplier(self):
		"""PI in Creditors (Liability) ↔ advance JE Dr in the advance account
		(Payable type, but Asset root). Exercises BOTH split branches in one
		reconcile (the Asset side and the Liability side resolve differently)."""
		self.supplier = make_supplier("_Test Supplier")
		amount = 1000

		pi = make_purchase_invoice(
			company=self.company,
			supplier=self.supplier,
			cost_center=self.cost_center,
			warehouse=self.warehouse,
			expense_account=self.expense_account,
			qty=1,
			rate=amount,
			item_code=self.item,
			do_not_save=True,
		)
		pi.credit_to = self.creditors  # Liability
		pi.save().submit()

		# Advance Dr in the Asset-root advance account.
		adv = self.create_journal_entry(self.advance_payable_account, self.bank, amount)
		adv.accounts[0].party_type = "Supplier"
		adv.accounts[0].party = self.supplier
		adv.save().submit()

		self._assert_cross_account_settled(self.supplier, False, recv_no=adv.name, pay_no=pi.name, invoice=pi)

	def test_cross_account_mixed_root_customer(self):
		"""SI in Debtors (Asset) ↔ advance JE Cr in the advance received account
		(Receivable type, Liability root)."""
		amount = 800

		si = create_sales_invoice(
			company=self.company,
			customer=self.customer,
			debit_to=self.debit_to,  # Asset
			cost_center=self.cost_center,
			warehouse=self.warehouse,
			income_account=self.income_account,
			expense_account=self.expense_account,
			qty=1,
			rate=amount,
			currency="INR",
		)

		# Advance Cr in the Liability-root advance account.
		adv = self.create_journal_entry(self.bank, self.advance_receivable_account, amount)
		adv.accounts[1].party_type = "Customer"
		adv.accounts[1].party = self.customer
		adv.save().submit()

		self._assert_cross_account_settled(self.customer, True, recv_no=si.name, pay_no=adv.name, invoice=si)

	def test_cross_account_negative_value_je_customer(self):
		"""Customer mirror: SI in a second Debtors ↔ advance booked as a NEGATIVE
		debit on the default Debtors (≡ a credit)."""
		debtors2 = self._make_account("Debtors New", "Accounts Receivable - _PR", "Receivable")
		amount = 500

		si = create_sales_invoice(
			company=self.company,
			customer=self.customer,
			debit_to=debtors2,
			cost_center=self.cost_center,
			warehouse=self.warehouse,
			income_account=self.income_account,
			expense_account=self.expense_account,
			qty=1,
			rate=amount,
			currency="INR",
		)

		# Negative debit on Debtors (≡ Cr Debtors 500) → advance/payment-like.
		adv = self.create_journal_entry(self.bank, self.debit_to, amount)
		adv.accounts[1].party_type = "Customer"
		adv.accounts[1].party = self.customer
		adv.accounts[1].credit_in_account_currency = 0
		adv.accounts[1].debit_in_account_currency = -amount
		adv.save().submit()

		self._assert_cross_account_settled(self.customer, True, recv_no=si.name, pay_no=adv.name, invoice=si)

	def test_cross_account_je_against_je_negative_leg(self):
		"""JE↔JE cross-account where the payment-like JE is booked as a negative
		credit instead of a debit."""
		self.supplier = make_supplier("_Test Supplier")
		creditors2 = self._make_account("Creditors New", "Accounts Payable - _PR", "Payable")
		amount = 700

		inv_je = self.create_journal_entry(self.expense_account, creditors2, amount)
		inv_je.accounts[1].party_type = "Supplier"
		inv_je.accounts[1].party = self.supplier
		inv_je.save().submit()

		# Payment-like booked as negative credit on Creditors (≡ Dr 700).
		pay_je = self.create_journal_entry(self.creditors, self.bank, amount)
		pay_je.accounts[0].party_type = "Supplier"
		pay_je.accounts[0].party = self.supplier
		pay_je.accounts[0].debit_in_account_currency = 0
		pay_je.accounts[0].credit_in_account_currency = -amount
		pay_je.save().submit()

		self._assert_cross_account_settled(self.supplier, False, recv_no=pay_je.name, pay_no=inv_je.name)

	def test_cross_account_pi_against_negative_credit_je(self):
		"""The reported case: book a PI, then a JE that settles it with a NEGATIVE
		credit on a different account. Negative credit ≡ debit → to_receive, pairs
		with the PI across accounts and bridges cleanly."""
		self.supplier = make_supplier("_Test Supplier")
		creditors2 = self._make_account("Creditors New", "Accounts Payable - _PR", "Payable")
		amount = 1000

		pi = make_purchase_invoice(
			company=self.company,
			supplier=self.supplier,
			cost_center=self.cost_center,
			warehouse=self.warehouse,
			expense_account=self.expense_account,
			qty=1,
			rate=amount,
			item_code=self.item,
			do_not_save=True,
		)
		pi.credit_to = creditors2
		pi.save().submit()

		# JE against the PI, booked as a negative credit on the default Creditors.
		je = self.create_journal_entry(self.creditors, self.bank, amount)
		je.accounts[0].party_type = "Supplier"
		je.accounts[0].party = self.supplier
		je.accounts[0].debit_in_account_currency = 0
		je.accounts[0].credit_in_account_currency = -amount
		je.save().submit()

		self._assert_cross_account_settled(self.supplier, False, recv_no=je.name, pay_no=pi.name, invoice=pi)

	def test_cross_account_multi_currency_supplier(self):
		"""Two USD payable accounts, advance & invoice booked at DIFFERENT rates.
		Cross-account bridge must settle both and book the FX gain/loss."""
		self.supplier = make_supplier("_Test Supplier USD", "USD")
		payable_usd2 = self._make_account(
			"Payable USD 2", "Accounts Payable - _PR", "Payable", currency="USD"
		)
		amount = 100
		rate_inv = 80
		rate_adv = 83

		# Invoice-like JE: Cr Payable USD 2 at rate_inv (to_pay).
		inv_je = frappe.new_doc("Journal Entry")
		inv_je.posting_date = nowdate()
		inv_je.company = self.company
		inv_je.multi_currency = 1
		inv_je.set(
			"accounts",
			[
				{
					"account": payable_usd2,
					"party_type": "Supplier",
					"party": self.supplier,
					"exchange_rate": rate_inv,
					"cost_center": self.cost_center,
					"credit": amount * rate_inv,
					"credit_in_account_currency": amount,
				},
				{
					"account": self.expense_account,
					"cost_center": self.cost_center,
					"debit": amount * rate_inv,
					"debit_in_account_currency": amount * rate_inv,
				},
			],
		)
		inv_je.save().submit()

		# Advance: Dr Payable USD at rate_adv (to_receive), different rate.
		adv = frappe.new_doc("Journal Entry")
		adv.posting_date = nowdate()
		adv.company = self.company
		adv.multi_currency = 1
		adv.set(
			"accounts",
			[
				{
					"account": self.creditors_usd,
					"party_type": "Supplier",
					"party": self.supplier,
					"exchange_rate": rate_adv,
					"cost_center": self.cost_center,
					"debit": amount * rate_adv,
					"debit_in_account_currency": amount,
				},
				{
					"account": self.cash,
					"cost_center": self.cost_center,
					"credit": amount * rate_adv,
					"credit_in_account_currency": amount * rate_adv,
				},
			],
		)
		adv.save().submit()

		pr = self._cross_account_pr(self.supplier, party_is_customer=False)
		pr.currency_filter = "USD"
		pr.get_unreconciled_entries()
		recv = [r for r in pr.to_receive if r.voucher_no == adv.name]
		pay = [r for r in pr.to_pay if r.voucher_no == inv_je.name]
		self.assertEqual(len(recv), 1)
		self.assertEqual(len(pay), 1)
		self.assertNotEqual(recv[0].account, pay[0].account)

		pr.allocate_entries(to_receive=[r.as_dict() for r in recv], to_pay=[r.as_dict() for r in pay])
		# Different rates → an FX difference must be computed (100 * (80 - 83)).
		self.assertEqual(flt(pr.allocation[0].difference_amount), -300)
		pr.reconcile()

		# Both settle.
		pr2 = self._cross_account_pr(self.supplier, party_is_customer=False)
		pr2.currency_filter = "USD"
		pr2.get_unreconciled_entries()
		self.assertNotIn(inv_je.name, {r.voucher_no for r in pr2.to_pay})
		self.assertNotIn(adv.name, {r.voucher_no for r in pr2.to_receive})

		# FX gain/loss JE was booked through the standard flow.
		fx_filters = {
			"voucher_type": "Exchange Gain Or Loss",
			"is_system_generated": 1,
			"company": self.company,
			"docstatus": 1,
		}
		self.assertTrue(
			frappe.db.exists("Journal Entry", fx_filters),
			"expected an Exchange Gain/Loss JE for the rate difference",
		)

		# Unreconcile must cancel the bridge AND the FX JE (the FX JE references the
		# bridge, so cancelling the bridge cascades to it) and reopen both sides.
		import json

		from erpnext.accounts.doctype.unreconcile_payment.unreconcile_payment import (
			create_unreconcile_doc_for_selection,
		)

		bridge = self._bridge_je_name()
		create_unreconcile_doc_for_selection(
			json.dumps(
				[
					{
						"company": self.company,
						"voucher_type": "Journal Entry",
						"voucher_no": bridge,
						"against_voucher_type": "Journal Entry",
						"against_voucher_no": adv.name,
					}
				]
			)
		)
		self.assertEqual(frappe.db.get_value("Journal Entry", bridge, "docstatus"), 2)
		self.assertFalse(
			frappe.db.exists("Journal Entry", fx_filters),
			"FX JE should have been cancelled with the bridge",
		)
		pr3 = self._cross_account_pr(self.supplier, party_is_customer=False)
		pr3.currency_filter = "USD"
		pr3.get_unreconciled_entries()
		open_vouchers = {r.voucher_no for r in pr3.to_receive} | {r.voucher_no for r in pr3.to_pay}
		self.assertIn(inv_je.name, open_vouchers)
		self.assertIn(adv.name, open_vouchers)

	def _setup_cross_account_reconciled_supplier(self, amount=1000):
		"""Case 3 reconciled: returns (pi, adv, bridge_name)."""
		self.supplier = make_supplier("_Test Supplier")
		creditors2 = self._make_account("Creditors New", "Accounts Payable - _PR", "Payable")

		pi = make_purchase_invoice(
			company=self.company,
			supplier=self.supplier,
			cost_center=self.cost_center,
			warehouse=self.warehouse,
			expense_account=self.expense_account,
			qty=1,
			rate=amount,
			item_code=self.item,
			do_not_save=True,
		)
		pi.credit_to = creditors2
		pi.save().submit()

		adv = self.create_journal_entry(self.creditors, self.bank, amount)
		adv.accounts[0].party_type = "Supplier"
		adv.accounts[0].party = self.supplier
		adv.save().submit()

		pr = self._cross_account_pr(self.supplier, party_is_customer=False)
		pr.get_unreconciled_entries()
		recv = [r for r in pr.to_receive if r.voucher_no == adv.name]
		pay = [r for r in pr.to_pay if r.voucher_no == pi.name]
		pr.allocate_entries(to_receive=[r.as_dict() for r in recv], to_pay=[r.as_dict() for r in pay])
		pr.reconcile()

		pi.reload()
		self.assertEqual(pi.outstanding_amount, 0)
		return pi, adv, self._bridge_je_name()

	def test_cross_account_cancel_invoice_cancels_bridge(self):
		"""Cancelling the bridged PI directly must auto-cancel the bridge JE and
		reopen the advance (the bridge exists only for this reconciliation)."""
		frappe.db.set_single_value("Accounts Settings", "unlink_payment_on_cancellation_of_invoice", 1)
		pi, adv, bridge = self._setup_cross_account_reconciled_supplier()
		self.assertTrue(bridge)

		pi.reload()
		pi.cancel()

		self.assertEqual(frappe.db.get_value("Journal Entry", bridge, "docstatus"), 2)
		# Advance reopened.
		pr = self._cross_account_pr(self.supplier, party_is_customer=False)
		pr.get_unreconciled_entries()
		self.assertIn(adv.name, {r.voucher_no for r in pr.to_receive})

	def test_cross_account_cancel_advance_cancels_bridge(self):
		"""Cancelling the bridged advance JE directly must auto-cancel the bridge JE
		and reopen the PI."""
		frappe.db.set_single_value("Accounts Settings", "unlink_payment_on_cancellation_of_invoice", 1)
		pi, adv, bridge = self._setup_cross_account_reconciled_supplier()
		self.assertTrue(bridge)

		adv.reload()
		adv.cancel()

		self.assertEqual(frappe.db.get_value("Journal Entry", bridge, "docstatus"), 2)
		# PI reopened.
		pi.reload()
		self.assertEqual(pi.outstanding_amount, 1000)

	def test_cross_account_delete_invoice_deletes_bridge(self):
		"""Deleting the invoice (with delete_linked_ledger_entries on) removes the
		cancelled bridge JE too — consistent with delete_exchange_gain_loss_journal."""
		frappe.db.set_single_value("Accounts Settings", "unlink_payment_on_cancellation_of_invoice", 1)
		frappe.db.set_single_value("Accounts Settings", "delete_linked_ledger_entries", 1)
		try:
			pi, adv, bridge = self._setup_cross_account_reconciled_supplier()
			self.assertTrue(bridge)

			pi.reload()
			pi.cancel()
			self.assertEqual(frappe.db.get_value("Journal Entry", bridge, "docstatus"), 2)

			pi.reload()
			pi.delete()
			# Cancelled bridge JE is gone, not left orphaned.
			self.assertFalse(frappe.db.exists("Journal Entry", bridge))
		finally:
			frappe.db.set_single_value("Accounts Settings", "delete_linked_ledger_entries", 0)

	def _bridge_je_name(self):
		return frappe.db.get_value(
			"Journal Entry",
			{"company": self.company, "voucher_type": "Reconciliation Journal", "docstatus": 1},
			"name",
		)

	def test_cross_account_unreconcile_cancels_bridge(self):
		"""Unreconciling a cross-account reconcile must CANCEL the system bridge JE
		and restore BOTH vouchers' outstanding — not leave the transfer half-applied."""
		import json

		from erpnext.accounts.doctype.unreconcile_payment.unreconcile_payment import (
			create_unreconcile_doc_for_selection,
		)

		self.supplier = make_supplier("_Test Supplier")
		creditors2 = self._make_account("Creditors New", "Accounts Payable - _PR", "Payable")
		amount = 1000

		pi = make_purchase_invoice(
			company=self.company,
			supplier=self.supplier,
			cost_center=self.cost_center,
			warehouse=self.warehouse,
			expense_account=self.expense_account,
			qty=1,
			rate=amount,
			item_code=self.item,
			do_not_save=True,
		)
		pi.credit_to = creditors2
		pi.save().submit()

		adv = self.create_journal_entry(self.creditors, self.bank, amount)
		adv.accounts[0].party_type = "Supplier"
		adv.accounts[0].party = self.supplier
		adv.save().submit()

		pr = self._cross_account_pr(self.supplier, party_is_customer=False)
		pr.get_unreconciled_entries()
		recv = [r for r in pr.to_receive if r.voucher_no == adv.name]
		pay = [r for r in pr.to_pay if r.voucher_no == pi.name]
		pr.allocate_entries(to_receive=[r.as_dict() for r in recv], to_pay=[r.as_dict() for r in pay])
		pr.reconcile()

		pi.reload()
		self.assertEqual(pi.outstanding_amount, 0)
		bridge = self._bridge_je_name()
		self.assertTrue(bridge, "bridge JE should exist after reconcile")

		# Unreconcile from the invoice side (the realistic UI entry point).
		create_unreconcile_doc_for_selection(
			json.dumps(
				[
					{
						"company": self.company,
						"voucher_type": "Journal Entry",
						"voucher_no": bridge,
						"against_voucher_type": "Purchase Invoice",
						"against_voucher_no": pi.name,
					}
				]
			)
		)

		# Bridge cancelled, PI fully open again.
		self.assertEqual(frappe.db.get_value("Journal Entry", bridge, "docstatus"), 2)
		pi.reload()
		self.assertEqual(pi.outstanding_amount, amount)

		# Advance JE reopened → reappears in a fresh fetch.
		pr2 = self._cross_account_pr(self.supplier, party_is_customer=False)
		pr2.get_unreconciled_entries()
		self.assertIn(adv.name, {r.voucher_no for r in pr2.to_receive})
		self.assertIn(pi.name, {r.voucher_no for r in pr2.to_pay})


def make_customer(customer_name, currency=None):
	if not frappe.db.exists("Customer", customer_name):
		customer = frappe.new_doc("Customer")
		customer.customer_name = customer_name
		customer.type = "Individual"

		if currency:
			customer.default_currency = currency
		customer.save()
		return customer.name
	else:
		return customer_name


def make_supplier(supplier_name, currency=None):
	if not frappe.db.exists("Supplier", supplier_name):
		supplier = frappe.new_doc("Supplier")
		supplier.supplier_name = supplier_name
		supplier.type = "Individual"

		if currency:
			supplier.default_currency = currency
		supplier.save()
		return supplier.name
	else:
		return supplier_name


def create_fiscal_year(company, year_start_date, year_end_date):
	fy_docname = frappe.db.exists(
		"Fiscal Year", {"year_start_date": year_start_date, "year_end_date": year_end_date}
	)
	if not fy_docname:
		fy_doc = frappe.get_doc(
			{
				"doctype": "Fiscal Year",
				"year": f"{getdate(year_start_date).year}-{getdate(year_end_date).year}",
				"year_start_date": year_start_date,
				"year_end_date": year_end_date,
				"companies": [{"company": company}],
			}
		).save()
		return fy_doc
	else:
		fy_doc = frappe.get_doc("Fiscal Year", fy_docname)
		if not frappe.db.exists("Fiscal Year Company", {"parent": fy_docname, "company": company}):
			fy_doc.append("companies", {"company": company})
			fy_doc.save()
		return fy_doc


def make_period_closing_voucher(company, cost_center, posting_date=None, submit=True):
	from erpnext.accounts.doctype.account.test_account import create_account

	parent_account = frappe.db.get_value(
		"Account", {"company": company, "account_name": "Current Liabilities", "is_group": 1}, "name"
	)
	surplus_account = create_account(
		account_name="Reserve and Surplus",
		is_group=0,
		company=company,
		root_type="Liability",
		report_type="Balance Sheet",
		account_currency="INR",
		parent_account=parent_account,
		doctype="Account",
	)
	fy = get_fiscal_year(posting_date, company=company)
	pcv = frappe.get_doc(
		{
			"doctype": "Period Closing Voucher",
			"transaction_date": posting_date or today(),
			"period_start_date": fy[1],
			"period_end_date": fy[2],
			"company": company,
			"fiscal_year": fy[0],
			"cost_center": cost_center,
			"closing_account_head": surplus_account,
			"remarks": "test",
		}
	)
	pcv.insert()
	if submit:
		pcv.submit()

	return pcv


class TestClassify(ERPNextTestSuite):
	def test_sales_invoice_regular_is_receivable(self):
		# SI: Dr Debtors → +ve on Receivable account
		self.assertEqual(classify("Receivable", 1000.0), "Receivable")

	def test_sales_invoice_credit_note_is_payable(self):
		# CN: Cr Debtors → -ve on Receivable account
		self.assertEqual(classify("Receivable", -500.0), "Payable")

	def test_purchase_invoice_regular_is_payable(self):
		# PI: Cr Creditors → +ve on Payable account
		self.assertEqual(classify("Payable", 1000.0), "Payable")

	def test_purchase_invoice_debit_note_is_receivable(self):
		# DN: Dr Creditors → -ve on Payable account
		self.assertEqual(classify("Payable", -500.0), "Receivable")

	def test_unsupported_account_type_throws(self):
		for bad in ("Asset", "Bank", "Cash", "Equity", "", None):
			with self.assertRaises(frappe.ValidationError):
				classify(bad, 100.0)

	def test_zero_amount_throws(self):
		with self.assertRaises(frappe.ValidationError):
			classify("Receivable", 0)
		with self.assertRaises(frappe.ValidationError):
			classify("Payable", 0.0)


class TestLinkStrategy(unittest.TestCase):
	"""Phase 2.6 — pure-function tests for the unified JE-creation dispatcher.

	No DB. Both helpers operate on dicts with a `voucher_type` key.
	"""

	def _pair(self, recv_type, pay_type):
		from erpnext.accounts.doctype.payment_reconciliation.payment_reconciliation import (
			link_strategy,
			pick_voucher_side,
		)

		return (
			frappe._dict(voucher_type=recv_type),
			frappe._dict(voucher_type=pay_type),
			link_strategy,
			pick_voucher_side,
		)

	# link_strategy — voucher_mutation when at least one side is PE/JE
	def test_si_pe_is_voucher_mutation(self):
		recv, pay, strategy, _ = self._pair("Sales Invoice", "Payment Entry")
		self.assertEqual(strategy(recv, pay), "voucher_mutation")

	def test_pi_pe_is_voucher_mutation(self):
		recv, pay, strategy, _ = self._pair("Payment Entry", "Purchase Invoice")
		self.assertEqual(strategy(recv, pay), "voucher_mutation")

	def test_je_pe_is_voucher_mutation(self):
		recv, pay, strategy, _ = self._pair("Journal Entry", "Payment Entry")
		self.assertEqual(strategy(recv, pay), "voucher_mutation")

	def test_pe_pe_is_voucher_mutation(self):
		recv, pay, strategy, _ = self._pair("Payment Entry", "Payment Entry")
		self.assertEqual(strategy(recv, pay), "voucher_mutation")

	def test_je_je_is_voucher_mutation(self):
		recv, pay, strategy, _ = self._pair("Journal Entry", "Journal Entry")
		self.assertEqual(strategy(recv, pay), "voucher_mutation")

	def test_si_je_is_voucher_mutation(self):
		recv, pay, strategy, _ = self._pair("Sales Invoice", "Journal Entry")
		self.assertEqual(strategy(recv, pay), "voucher_mutation")

	# link_strategy — bridge_je only when neither side has writable refs
	def test_si_si_cn_is_bridge_je(self):
		# Customer SI x CN: regular SI on receive, CN (also Sales Invoice) on pay
		recv, pay, strategy, _ = self._pair("Sales Invoice", "Sales Invoice")
		self.assertEqual(strategy(recv, pay), "bridge_je")

	def test_pi_pi_dn_is_bridge_je(self):
		# Supplier PI x DN: regular PI on pay, DN (also Purchase Invoice) on receive
		recv, pay, strategy, _ = self._pair("Purchase Invoice", "Purchase Invoice")
		self.assertEqual(strategy(recv, pay), "bridge_je")

	def test_si_pi_is_bridge_je(self):
		# Cross-doctype invoice pair (would only arise via cross-party Common Party)
		recv, pay, strategy, _ = self._pair("Sales Invoice", "Purchase Invoice")
		self.assertEqual(strategy(recv, pay), "bridge_je")

	# pick_voucher_side — PE > JE > others
	def test_pick_pe_wins_over_si(self):
		recv, pay, _, pick = self._pair("Sales Invoice", "Payment Entry")
		self.assertEqual(pick(recv, pay), "pay")

	def test_pick_pe_on_receive_wins(self):
		recv, pay, _, pick = self._pair("Payment Entry", "Purchase Invoice")
		self.assertEqual(pick(recv, pay), "receive")

	def test_pick_pe_wins_over_je(self):
		recv, pay, _, pick = self._pair("Journal Entry", "Payment Entry")
		self.assertEqual(pick(recv, pay), "pay")

	def test_pick_pe_on_receive_wins_over_je(self):
		recv, pay, _, pick = self._pair("Payment Entry", "Journal Entry")
		self.assertEqual(pick(recv, pay), "receive")

	def test_pick_je_when_no_pe(self):
		recv, pay, _, pick = self._pair("Sales Invoice", "Journal Entry")
		self.assertEqual(pick(recv, pay), "pay")

	def test_pick_je_on_receive_when_no_pe(self):
		recv, pay, _, pick = self._pair("Journal Entry", "Sales Invoice")
		self.assertEqual(pick(recv, pay), "receive")

	def test_pick_bridge_je_pair_asserts(self):
		recv, pay, _, pick = self._pair("Sales Invoice", "Sales Invoice")
		# `pick_voucher_side` guards an SIxSI (bridge) pair with frappe.throw,
		# which raises ValidationError (not a bare assert).
		with self.assertRaises(frappe.ValidationError):
			pick(recv, pay)


class TestReconcileRouterArgs(unittest.TestCase):
	"""M6 — pure-function test: `_build_payment_args` shape for representative
	Phase 1 scenarios. No DB; constructs a stub PR.
	"""

	def _router(self, party_type="Customer"):
		from erpnext.accounts.doctype.payment_reconciliation.payment_reconciliation import (
			ReconcileRouter,
		)

		# Stub PR: only the attributes ReconcileRouter reads.
		pr = frappe._dict(
			company="_Test Payment Reconciliation",
			party_type=party_type,
			party="_Test Party",
			receivable_payable_account="Debtors - _PR" if party_type == "Customer" else "Creditors - _PR",
			dimensions=[],
		)
		# erpnext.get_party_account_type expects a real party_type; both Customer
		# (Receivable) and Supplier (Payable) are real values, so this works.
		return ReconcileRouter(pr)

	def _router_args(self, alloc_row, party_type="Customer"):
		from erpnext.accounts.doctype.payment_reconciliation.payment_reconciliation import (
			link_strategy,
		)

		router = self._router(party_type)
		recv = frappe._dict(voucher_type=alloc_row["to_receive_voucher_type"])
		pay = frappe._dict(voucher_type=alloc_row["to_pay_voucher_type"])
		strategy = link_strategy(recv, pay)
		return router._build_payment_args(frappe._dict(alloc_row)), strategy

	def test_args_si_pe_customer(self):
		args, strategy = self._router_args(
			{
				"to_receive_voucher_type": "Sales Invoice",
				"to_receive_voucher_no": "SI-1",
				"to_receive_account": "Debtors - _PR",
				"to_pay_voucher_type": "Payment Entry",
				"to_pay_voucher_no": "PE-1",
				"to_pay_account": "Debtors - _PR",
				"allocated_amount": 100,
				"unreconciled_amount": 100,
				"amount": 100,
				"exchange_rate": 1,
			}
		)
		self.assertEqual(strategy, "voucher_mutation")
		# PE wins voucher slot
		self.assertEqual(args.voucher_type, "Payment Entry")
		self.assertEqual(args.voucher_no, "PE-1")
		self.assertEqual(args.against_voucher_type, "Sales Invoice")
		self.assertEqual(args.against_voucher, "SI-1")

	def test_args_pi_pe_supplier(self):
		args, strategy = self._router_args(
			{
				"to_receive_voucher_type": "Payment Entry",
				"to_receive_voucher_no": "PE-1",
				"to_receive_account": "Creditors - _PR",
				"to_pay_voucher_type": "Purchase Invoice",
				"to_pay_voucher_no": "PI-1",
				"to_pay_account": "Creditors - _PR",
				"allocated_amount": 100,
				"unreconciled_amount": 100,
				"amount": 100,
				"exchange_rate": 1,
			},
			party_type="Supplier",
		)
		self.assertEqual(strategy, "voucher_mutation")
		# PE on receive side wins
		self.assertEqual(args.voucher_no, "PE-1")
		self.assertEqual(args.against_voucher, "PI-1")

	def test_args_je_pe_customer_pe_wins(self):
		"""PR-B regression: PE > JE in `pick_voucher_side`."""
		args, strategy = self._router_args(
			{
				"to_receive_voucher_type": "Journal Entry",
				"to_receive_voucher_no": "JE-1",
				"to_receive_account": "Debtors - _PR",
				"to_pay_voucher_type": "Payment Entry",
				"to_pay_voucher_no": "PE-1",
				"to_pay_account": "Debtors - _PR",
				"allocated_amount": 100,
				"unreconciled_amount": 100,
				"amount": 100,
				"exchange_rate": 1,
			}
		)
		self.assertEqual(strategy, "voucher_mutation")
		self.assertEqual(args.voucher_no, "PE-1")
		self.assertEqual(args.against_voucher, "JE-1")

	def test_args_si_cn_customer_bridge_must_be_bridged_first(self):
		"""A both-non-writable pair (SIxCN) is still classified `bridge_je`, but it is
		re-expressed against a bridge JE BEFORE reaching `_build_payment_args`. Calling
		`_build_payment_args` on the raw pair has no PE/JE slot to pick, so
		`pick_voucher_side` raises — it must never be reached un-bridged."""
		from erpnext.accounts.doctype.payment_reconciliation.payment_reconciliation import (
			link_strategy,
		)

		recv = frappe._dict(voucher_type="Sales Invoice")
		pay = frappe._dict(voucher_type="Sales Invoice")  # CN
		self.assertEqual(link_strategy(recv, pay), "bridge_je")

		router = self._router("Customer")
		with self.assertRaises(frappe.ValidationError):
			router._build_payment_args(
				frappe._dict(
					{
						"to_receive_voucher_type": "Sales Invoice",
						"to_receive_voucher_no": "SI-1",
						"to_receive_account": "Debtors - _PR",
						"to_pay_voucher_type": "Sales Invoice",  # CN
						"to_pay_voucher_no": "CN-1",
						"to_pay_account": "Debtors - _PR",
						"allocated_amount": 50,
						"unreconciled_amount": 50,
						"amount": 50,
						"exchange_rate": 1,
					}
				)
			)

	def test_args_pi_dn_supplier_bridge_must_be_bridged_first(self):
		"""Supplier counterpart of the above: a PIxDN pair is bridged before
		`_build_payment_args`, which raises if handed the raw pair."""
		from erpnext.accounts.doctype.payment_reconciliation.payment_reconciliation import (
			link_strategy,
		)

		recv = frappe._dict(voucher_type="Purchase Invoice")  # DN
		pay = frappe._dict(voucher_type="Purchase Invoice")
		self.assertEqual(link_strategy(recv, pay), "bridge_je")

		router = self._router("Supplier")
		with self.assertRaises(frappe.ValidationError):
			router._build_payment_args(
				frappe._dict(
					{
						"to_receive_voucher_type": "Purchase Invoice",  # DN
						"to_receive_voucher_no": "DN-1",
						"to_receive_account": "Creditors - _PR",
						"to_pay_voucher_type": "Purchase Invoice",
						"to_pay_voucher_no": "PI-1",
						"to_pay_account": "Creditors - _PR",
						"allocated_amount": 30,
						"unreconciled_amount": 30,
						"amount": 30,
						"exchange_rate": 1,
					}
				)
			)
