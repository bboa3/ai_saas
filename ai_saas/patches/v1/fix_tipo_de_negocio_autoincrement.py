"""Fix Tipo de Negocio name column and sequence (managed by ensure_child_doctypes, not a JSON file)."""

import frappe


def execute():
	table = "tabTipo de Negocio"
	seq_name = "tipo_de_negocio_id_seq"
	doctype = "Tipo de Negocio"

	if not frappe.db.exists("DocType", doctype):
		return

	col_info = frappe.db.sql(
		"SELECT DATA_TYPE FROM information_schema.COLUMNS "
		"WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME='name'",
		table,
	)
	if not col_info or col_info[0][0].lower() not in ("varchar", "char"):
		return

	frappe.db.sql(f"ALTER TABLE `{table}` MODIFY COLUMN `name` bigint(20) NOT NULL")

	seq_exists = frappe.db.sql(
		"SELECT TABLE_NAME FROM information_schema.TABLES "
		"WHERE TABLE_SCHEMA=DATABASE() AND TABLE_TYPE='SEQUENCE' AND TABLE_NAME=%s",
		seq_name,
	)
	if not seq_exists:
		frappe.db.sql(
			f"CREATE SEQUENCE `{seq_name}` START WITH 1 INCREMENT BY 1 NOCACHE ENGINE=InnoDB"
		)

	frappe.db.set_value("DocType", doctype, "autoname", "autoincrement")
	frappe.db.commit()
	print(f"  Fixed: {table} → name column converted to bigint, sequence {seq_name} ensured")
