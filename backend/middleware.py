class SecurityHeadersMiddleware:
    """Запрещает скрипты на служебных страницах (вход, пароль, загрузка копии). Страница CRM задаёт свою политику."""

    POLICY = (
        "default-src 'self'; script-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault("Content-Security-Policy", self.POLICY)
        response.setdefault("Referrer-Policy", "same-origin")
        return response
