"""
Fix child DocTables that were originally created with autoname=field:X
and later changed to autoname=autoincrement.

Frappe's sync_all() updates the DocType meta but never alters the name
column type or creates the sequence for existing tables. This patch does
both for every affected table on any site where migrate is run.
"""

import frappe


# Maps table name → sequence name (frappe.scrub(doctype) + "_id_seq")
AFFECTED_TABLES = {
	"tabConformidade MZ": "conformidade_mz_id_seq",
	"tabModulo ERPNext": "modulo_erpnext_id_seq",
	"tabIntegracao Recomendada": "integracao_recomendada_id_seq",
}


def execute():
	for table, seq_name in AFFECTED_TABLES.items():
		_fix_table(table, seq_name)


def _fix_table(table, seq_name):
	# Skip if the table doesn't exist on this site
	existing = frappe.db.sql(
		"SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
		table,
	)
	if not existing:
		return

	# Check current name column type
	col_info = frappe.db.sql(
		"SELECT DATA_TYPE FROM information_schema.COLUMNS "
		"WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME='name'",
		table,
	)
	if not col_info:
		return

	data_type = col_info[0][0].lower()

	# Only act if name is still varchar (not yet converted to bigint)
	if data_type not in ("varchar", "char"):
		return

	# Alter name column to bigint
	frappe.db.sql(f"ALTER TABLE `{table}` MODIFY COLUMN `name` bigint(20) NOT NULL")

	# Create the sequence if it doesn't already exist
	seq_exists = frappe.db.sql(
		"SELECT TABLE_NAME FROM information_schema.TABLES "
		"WHERE TABLE_SCHEMA=DATABASE() AND TABLE_TYPE='SEQUENCE' AND TABLE_NAME=%s",
		seq_name,
	)
	if not seq_exists:
		frappe.db.sql(
			f"CREATE SEQUENCE `{seq_name}` START WITH 1 INCREMENT BY 1 NOCACHE ENGINE=InnoDB"
		)

	frappe.db.commit()
	print(f"  Fixed: {table} → name column converted to bigint, sequence {seq_name} ensured")
