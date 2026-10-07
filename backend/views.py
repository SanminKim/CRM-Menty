"""Страницы: сама CRM, вход, смена пароля, загрузка резервной копии."""
import base64
import hashlib
from pathlib import Path

from django.conf import settings
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import render
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.safestring import mark_safe
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie

from . import store
from .auth import find_user, login_name
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
    "style-src 'self' 'unsafe-inline'; font-src 'self'; "
    "img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


def _hash(script):
    return "'sha256-" + base64.b64encode(hashlib.sha256(script.encode()).digest()).decode() + "'"


# Страницы входа и смены пароля выполняют один маленький скрипт («показать пароль», Caps Lock) и больше никакой
FORM_JS = mark_safe((Path(__file__).resolve().parent / "templates" / "backend" / "form.js").read_text(encoding="utf-8"))
CSP_FORM = (
    f"default-src 'self'; script-src {_hash(FORM_JS)}; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)


class FormPage:
    """Страница с формой пароля: отдаёт скрипт формы и разрешает выполнять только его."""

    def get_context_data(self, **kwargs):
        return {**super().get_context_data(**kwargs), "form_js": FORM_JS}

    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        response["Content-Security-Policy"] = CSP_FORM
        return response


# ---------- Шрифт страницы: лежит на своём сервере, а не у Google ----------

FONTS_DIR = WEB_DIR / "fonts"
FONTS = ("golos-text-cyrillic.woff2", "golos-text-latin.woff2", "golos-text-latin-ext.woff2")


def font(request, name):
    if name not in FONTS:
        raise Http404
    response = FileResponse(open(FONTS_DIR / name, "rb"), content_type="font/woff2")
    response["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


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
MISS_TTL = 24 * 3600     # сколько администратор видит неудачные входы


def miss_key(user):
    """Счётчик неудачных входов для экрана «Доступ». Сам введённый логин не хранится: в это поле по ошибке вводят и пароль."""
    return f"login-miss:{user.pk}" if user is not None else "login-miss:unknown"


def login_misses(user):
    return cache.get(miss_key(user))


def client_ip(request):
    # Caddy дописывает настоящий адрес клиента последним в X-Forwarded-For
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (forwarded.split(",")[-1].strip() if forwarded else "") or request.META.get("REMOTE_ADDR", "")


def _bump(key):
    # Срок хранения задаётся при каждой записи: у счётчика в базе incr сбросил бы его на срок по умолчанию
    cache.set(key, (cache.get(key) or 0) + 1, LOGIN_WINDOW)


class LoginView(FormPage, auth_views.LoginView):
    template_name = "backend/login.html"
    redirect_authenticated_user = True

    def _keys(self):
        ip = client_ip(self.request)
        if ":" in ip:
            ip = ":".join(ip.split(":")[:4])  # IPv6: считаем по подсети /64, иначе адрес легко менять
        # Логин в ключе счётчика — отпечатком: в ключ кэша не попадают пробелы и слишком длинные строки
        username = hashlib.sha256(login_name(self.request.POST.get("username", "")).lower().encode()).hexdigest()[:32]
        return f"login:{ip}:{username}", f"login-ip:{ip}"

    def post(self, request, *args, **kwargs):
        pair, by_ip = self._keys()
        if (cache.get(pair) or 0) >= LOGIN_TRIES or (cache.get(by_ip) or 0) >= LOGIN_TRIES_IP:
            return render(request, self.template_name, {"locked": True, "next": request.POST.get("next", ""), "form_js": FORM_JS}, status=429)
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        for key in self._keys():
            _bump(key)
        # Администратор увидит в «Доступе», у кого не получается войти: это отличает забытый пароль от неверного логина
        key = miss_key(find_user(self.request.POST.get("username", "")))
        cache.set(key, {"n": min((cache.get(key) or {}).get("n", 0) + 1, 999), "at": timezone.now().isoformat()}, MISS_TTL)
        return super().form_invalid(form)

    def form_valid(self, form):
        cache.delete(self._keys()[0])
        cache.delete(miss_key(form.get_user()))
        return super().form_valid(form)


class PasswordChangeView(FormPage, auth_views.PasswordChangeView):
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
                                      with_demo=bool(request.POST.get("with_demo")),
                                      passphrase=request.POST.get("passphrase") or None)
                ctx["result"] = summary_lines(stats)
            except BackupError as exc:
                ctx["error"] = str(exc)
            except store.Invalid as exc:
                ctx["error"] = f"В копии есть запись, которую нельзя сохранить: {exc}"
    return render(request, "backend/backup_import.html", ctx)
