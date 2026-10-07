"""Вход по логину без учёта регистра и случайных пробелов: «Admin », «admin» и «ADMIN» — одна учётная запись."""
import unicodedata

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


def login_name(raw):
    """Логин в том виде, в каком он сравнивается: без пробелов по краям и в единой форме Юникода."""
    return unicodedata.normalize("NFKC", str(raw or "")).strip()


def find_user(raw):
    """Учётная запись по логину. Точное совпадение важнее совпадения без учёта регистра."""
    name = login_name(raw)
    if not name or len(name) > 150:
        return None
    users = list(get_user_model().objects.filter(username__iexact=name)[:3])
    exact = [u for u in users if u.username == name]
    if exact:
        return exact[0]
    # Два логина, отличающихся только регистром, новая CRM не создаёт; если такие остались, вход только точным написанием
    return users[0] if len(users) == 1 else None


class LoginBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        if username is None or password is None:
            return None
        user = find_user(username)
        if user is None:
            get_user_model()().set_password(password)  # то же время ответа, что и при неверном пароле
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
