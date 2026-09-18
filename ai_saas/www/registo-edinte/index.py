"""Partner signup form — accounts on [slug].erp.edinte.co.mz. Same page as /registo."""

from ai_saas.www.registo.index import build_context

no_cache = 1
no_breadcrumbs = 1


def get_context(context):
	return build_context(context, ".erp.edinte.co.mz")
