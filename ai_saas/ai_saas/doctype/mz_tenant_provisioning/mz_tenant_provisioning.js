// C1 (docs/sales-funnel-implementation.md): a Failed provisioning is no longer dead —
// the button hands the record its attempts back and re-queues it.
frappe.ui.form.on("MZ Tenant Provisioning", {
	refresh(frm) {
		if (frm.doc.status !== "Failed" || frm.is_new()) return;
		frm.add_custom_button(__("Tentar Novamente"), () => {
			frappe.confirm(
				__("Repor o contador de tentativas e voltar a enfileirar o provisionamento de {0}?", [
					frm.doc.site_name,
				]),
				() => {
					frappe.call({
						method: "ai_saas.saas.provisioning.retry_provisioning",
						args: { name: frm.doc.name },
						freeze: true,
						freeze_message: __("A enfileirar..."),
						callback: () => {
							frappe.show_alert({ message: __("Provisionamento enfileirado"), indicator: "green" });
							frm.reload_doc();
						},
					});
				}
			);
		}).addClass("btn-primary");
	},
});
