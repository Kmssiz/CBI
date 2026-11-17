"""Table length chosen by the user: ?per_page=25, then remembered for the whole session."""
from django.core.paginator import Page, Paginator

PAGE_SIZES = (10, 25, 50, 100, 200)
DEFAULT_PAGE_SIZE = 10
SESSION_KEY = "table_page_size"


def page_size(request, default: int = DEFAULT_PAGE_SIZE) -> int:
    """The requested size (kept in the session), clamped to the offered choices; `default` when none was ever chosen."""
    asked = request.GET.get("per_page")
    if asked is not None:
        try:
            size = int(asked)
        except (TypeError, ValueError):
            size = 0
        if size in PAGE_SIZES:
            request.session[SESSION_KEY] = size
    stored = request.session.get(SESSION_KEY)
    return stored if stored in PAGE_SIZES else default


def paginate(request, items, default: int = DEFAULT_PAGE_SIZE):
    """(page, page_range) of `items` using the user's table length."""
    paginator = Paginator(items, page_size(request, default))
    page: Page = paginator.get_page(request.GET.get("page"))
    return page, paginator.get_elided_page_range(page.number, on_each_side=2, on_ends=1)
