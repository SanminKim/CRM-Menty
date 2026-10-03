"""JSON API для страницы CRM. Все запросы — от вошедшего пользователя, запись защищена CSRF."""
import json
import re
from functools import wraps

from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from . import store
from .models import Account, Doc

User = get_user_model()


def api(view):
    """Вход обязателен, роль обязательна; ошибки хранилища превращаются в коды ответа."""
    @never_cache
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        access = store.Access(request.user)
        if access.role is None:
            return JsonResponse({"error": "unauthorized"}, status=401)
        try:
            return view(request, access, *args, **kwargs)
        except store.Denied:
            return JsonResponse({"error": "permission_denied"}, status=403)
        except store.NotFound:
            return JsonResponse({"error": "not_found"}, status=404)
        except store.Invalid as exc:
            return JsonResponse({"error": "invalid_argument", "message": str(exc)}, status=400)
    return wrapper


def body(request):
    if len(request.body) > store.MAX_DOC_BYTES * 2:
        raise store.Invalid("Запрос слишком большой")
    try:
        data = json.loads(request.body or b"{}")
    except (ValueError, RecursionError):
        raise store.Invalid("Некорректный JSON")
    if not isinstance(data, dict):
        raise store.Invalid("Ожидается объект")
    return data


def display_name(user):
    return user.get_full_name() or user.get_username()


@require_GET
@api
def me(request, access):
    return JsonResponse({
        "id": store.user_key(request.user), "name": display_name(request.user), "username": request.user.get_username(),
        "role": access.role, "isOwner": access.is_admin, "canWrite": access.role != Account.Role.PARTNER,
        "linkId": access.link_id, "server": True,
    })


@require_GET
@api
def sync(request, access):
    def num(name):
        try:
            return int(request.GET[name])
        except (KeyError, ValueError):
            return None
    return JsonResponse(store.snapshot(access, since=num("since"), epoch=num("epoch")))


@require_http_methods(["PUT", "PATCH", "DELETE"])
@api
def doc(request, access, collection, doc_id):
    if request.method == "DELETE":
        return JsonResponse({"rev": store.delete(access, collection, doc_id)})
    rev, data = store.write(access, collection, doc_id, body(request), partial=request.method == "PATCH")
    return JsonResponse({"rev": rev, "doc": data})


@require_GET
@api
def profiles(request, access):
    """Имена людей по их ключам: авторы заметок и смен этапов. Партнёру имена сотрудников не отдаём."""
    if access.role == Account.Role.PARTNER:
        return JsonResponse({})
    ids = [i for i in request.GET.get("ids", "").split(",") if i][:100]
    out = {}
    pks = [int(i[1:]) for i in ids if re.fullmatch(r"u[0-9]{1,15}", i)]
    # Только сотрудники школы: учётные записи партнёров по этому адресу не раскрываются
    staff = User.objects.filter(pk__in=pks).exclude(account__role=Account.Role.PARTNER)
    for user in staff.filter(account__isnull=False) | staff.filter(is_superuser=True):
        out[store.user_key(user)] = {"name": display_name(user)}
    # Авторы из версии в Claude: их ключи запомнены в записях команды при импорте копии
    legacy = {d.data.get("legacyUserId"): d.data.get("name") for d in Doc.objects.filter(collection="team")}
    for i in ids:
        if i not in out and legacy.get(i):
            out[i] = {"name": legacy[i]}
    return JsonResponse(out)


# ---------- Учётные записи (только администратор) ----------

def account_json(user):
    acc = getattr(user, "account", None)
    return {
        "id": user.pk, "username": user.get_username(), "name": user.get_full_name(), "active": user.is_active,
        "role": acc.role if acc else (Account.Role.ADMIN if user.is_superuser else ""),
        "linkId": acc.link_id if acc else "", "me": False,
    }


def _apply_account(user, data, creating):
    username = str(data.get("username", user.username or "")).strip()
    if not username or len(username) > 150:
        raise store.Invalid("Укажите логин")
    if User.objects.filter(username__iexact=username).exclude(pk=user.pk).exists():
        raise store.Invalid("Такой логин уже есть")
    role = data.get("role", getattr(getattr(user, "account", None), "role", ""))
    if role not in Account.Role.values:
        raise store.Invalid("Выберите роль")
    link_id = str(data.get("linkId", getattr(getattr(user, "account", None), "link_id", "")) or "")
    if link_id:
        collection = "partners" if role == Account.Role.PARTNER else "team"
        if not Doc.objects.filter(collection=collection, doc_id=link_id).exists():
            raise store.Invalid("Связанная запись не найдена")
        clash = Account.objects.filter(link_id=link_id, role=Account.Role.PARTNER) if role == Account.Role.PARTNER \
            else Account.objects.filter(link_id=link_id).exclude(role=Account.Role.PARTNER)
        if clash.exclude(user_id=user.pk).exists():
            raise store.Invalid("С этой записью уже связана другая учётная запись")
    elif role != Account.Role.ADMIN:
        raise store.Invalid("Выберите, с кем связана учётная запись")
    password = data.get("password") or ""
    if creating and not password:
        raise store.Invalid("Задайте пароль")
    user.username = username
    name = str(data.get("name", user.get_full_name())).strip()
    user.first_name, _, user.last_name = name[:300].partition(" ")
    user.first_name, user.last_name = user.first_name[:150], user.last_name[:150]
    if "active" in data:
        user.is_active = bool(data["active"])
    if password:
        try:
            validate_password(password, user)
        except ValidationError as exc:
            raise store.Invalid(" ".join(exc.messages))
        user.set_password(password)
    user.save()
    Account.objects.update_or_create(user=user, defaults={"role": role, "link_id": link_id})


@require_http_methods(["GET", "POST"])
@api
def accounts(request, access):
    if not access.is_admin:
        raise store.Denied
    if request.method == "POST":
        with transaction.atomic():
            store.bump_epoch()  # первой берётся блокировка состояния: тот же порядок, что и при записи документов
            user = User()
            _apply_account(user, body(request), creating=True)
        return JsonResponse(account_json(user), status=201)
    users = User.objects.select_related("account").order_by("username")
    rows = [account_json(u) for u in users if hasattr(u, "account") or u.is_superuser]
    for row in rows:
        row["me"] = row["id"] == request.user.pk
    return JsonResponse({"accounts": rows})


@require_http_methods(["PATCH"])
@api
def account(request, access, pk):
    if not access.is_admin:
        raise store.Denied
    user = User.objects.select_related("account").filter(pk=pk).first()
    if user is None:
        raise store.NotFound
    data = body(request)
    if user.pk == request.user.pk and (data.get("active") is False or data.get("role", "admin") != "admin"):
        raise store.Invalid("Нельзя отключить себя или снять с себя роль администратора")
    with transaction.atomic():
        store.bump_epoch()
        _apply_account(user, data, creating=False)
    if user.pk == request.user.pk and data.get("password"):
        update_session_auth_hash(request, user)  # смена своего пароля не выкидывает из системы
    return JsonResponse(account_json(user))
