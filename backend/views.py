"""Страницы: сама CRM, вход, смена пароля, загрузка резервной копии."""
import base64
import hashlib
import unicodedata
from pathlib import Path

from django.conf import settings
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse_lazy
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie

from . import store
from .backup import BackupError, import_backup, summary_lines

WEB_DIR = Path(settings.BASE_DIR) / "web"
ICON = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32"
    "' height='32' rx='7' fill='%23ffd21f'/%3E%3Ctext x='16' y='23' font-family='Arial,sans-serif' font-size='19' font-weight"
    "='700' text-anchor='middle' fill='%231b1a17'%3E%D0%9C%3C/text%3E%3C/svg%3E"
)
_page = {"key": None, "html": "", "csp": ""}
CSP_PAGE = (
    "default-src 'self'; script-src {hashes}; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
    "img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


def _hash(script):
    return "'sha256-" + base64.b64encode(hashlib.sha256(script.encode()).digest()).decode() + "'"


def build_page():
    """Страница CRM: web/index.html с подключённым web/server.js. Пересобирается, когда файлы меняются."""
    index, shim = WEB_DIR / "index.html", WEB_DIR / "server.js"
    key = (index.stat().st_mtime_ns, shim.stat().st_mtime_ns)
    if _page["key"] != key:
        shim_js, page = f"\n{shim.read_text(encoding='utf-8')}\n", index.read_text(encoding="utf-8")
        _page["html"] = (
            '<!doctype html>\n<html lang="ru">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<meta name="robots" content="noindex, nofollow">\n'
            f'<link rel="icon" href="{ICON}">\n'
            f"<script>{shim_js}</script>\n</head>\n<body>\n{page}\n</body>\n</html>\n"
        )
        # Браузер выполнит только эти два скрипта: вставленный в страницу чужой скрипт не запустится
        scripts = [shim_js] + [part.split("</script>")[0] for part in page.split("<script>")[1:]]
        _page["csp"] = CSP_PAGE.format(hashes=" ".join(_hash(s) for s in scripts))
        _page["key"] = key
    return _page["html"], _page["csp"]


@never_cache
@ensure_csrf_cookie
@login_required
def app(request):
    if store.Access(request.user).role is None:
        return render(request, "backend/noaccess.html", status=403)
    html, csp = build_page()
    response = HttpResponse(html)
    response["Content-Security-Policy"] = csp
    return response


# ---------- Вход с ограничением числа попыток ----------

LOGIN_TRIES = 8          # неудачных попыток на пару «адрес + логин»
LOGIN_TRIES_IP = 40      # и на один адрес
LOGIN_WINDOW = 15 * 60   # секунд


def client_ip(request):
    # Caddy дописывает настоящий адрес клиента последним в X-Forwarded-For
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (forwarded.split(",")[-1].strip() if forwarded else "") or request.META.get("REMOTE_ADDR", "")


def _bump(key):
    # Срок хранения задаётся при каждой записи: у счётчика в базе incr сбросил бы его на срок по умолчанию
    cache.set(key, (cache.get(key) or 0) + 1, LOGIN_WINDOW)


class LoginView(auth_views.LoginView):
    template_name = "backend/login.html"
    redirect_authenticated_user = True

    def _keys(self):
        ip = client_ip(self.request)
        if ":" in ip:
            ip = ":".join(ip.split(":")[:4])  # IPv6: считаем по подсети /64, иначе адрес легко менять
        username = unicodedata.normalize("NFKC", self.request.POST.get("username", "")).strip().lower()[:150]
        return f"login:{ip}:{username}", f"login-ip:{ip}"

    def post(self, request, *args, **kwargs):
        pair, by_ip = self._keys()
        if (cache.get(pair) or 0) >= LOGIN_TRIES or (cache.get(by_ip) or 0) >= LOGIN_TRIES_IP:
            return render(request, self.template_name, {"locked": True, "next": request.POST.get("next", "")}, status=429)
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        for key in self._keys():
            _bump(key)
        return super().form_invalid(form)

    def form_valid(self, form):
        cache.delete(self._keys()[0])
        return super().form_valid(form)


class PasswordChangeView(auth_views.PasswordChangeView):
    template_name = "backend/password.html"
    success_url = reverse_lazy("password_done")


@login_required
def password_done(request):
    return render(request, "backend/password.html", {"done": True})


@never_cache
@login_required
def backup_import(request):
    """Перенос данных из версии в Claude: загрузка файла резервной копии. Только администратор."""
    if not store.Access(request.user).is_admin:
        return render(request, "backend/noaccess.html", status=403)
    ctx = {}
    if request.method == "POST":
        upload = request.FILES.get("file")
        if not upload:
            ctx["error"] = "Выберите файл резервной копии"
        elif upload.size > 20 * 1024 * 1024:
            ctx["error"] = "Файл больше 20 МБ"
        else:
            try:
                stats = import_backup(upload.read(), overwrite=bool(request.POST.get("overwrite")),
                                      with_demo=bool(request.POST.get("with_demo")))
                ctx["result"] = summary_lines(stats)
            except BackupError as exc:
                ctx["error"] = str(exc)
            except store.Invalid as exc:
                ctx["error"] = f"В копии есть запись, которую нельзя сохранить: {exc}"
    return render(request, "backend/backup_import.html", ctx)
