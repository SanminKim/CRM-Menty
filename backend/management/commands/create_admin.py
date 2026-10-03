"""Первый администратор: python manage.py create_admin логин [--name "Имя Фамилия"]. Пароль спрашивается."""
import getpass
import os

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from backend import store
from backend.models import Account


class Command(BaseCommand):
    help = "Создаёт администратора CRM или сбрасывает ему пароль"

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--name", default="")

    def handle(self, *args, username, name, **opts):
        # Пароль берётся из переменной окружения (для скриптов) или спрашивается с клавиатуры
        password = os.environ.get("CRM_ADMIN_PASSWORD") or getpass.getpass("Пароль: ")
        User = get_user_model()
        user = User.objects.filter(username__iexact=username).first() or User(username=username)
        try:
            validate_password(password, user)
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages))
        if name:
            user.first_name, _, user.last_name = name.partition(" ")
        user.is_active = True
        user.set_password(password)
        user.save()
        Account.objects.update_or_create(user=user, defaults={"role": Account.Role.ADMIN})
        store.bump_epoch()
        self.stdout.write(f"Администратор «{user.get_username()}» готов. Войдите на главной странице CRM.")
