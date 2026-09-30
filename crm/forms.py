from django import forms
from django.contrib.auth import get_user_model

from .models import JobApplication, Note, Payment, PartnerPayout, Student
from .permissions import ADMIN_GROUP, MENTOR_GROUP


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, **kwargs):
        super().__init__(format="%Y-%m-%d", **kwargs)


class DateTimeInput(forms.DateTimeInput):
    input_type = "datetime-local"

    def __init__(self, **kwargs):
        super().__init__(format="%Y-%m-%dT%H:%M", **kwargs)


def staff_users():
    from django.db.models import Q
    return get_user_model().objects.filter(
        Q(groups__name__in=[ADMIN_GROUP, MENTOR_GROUP]) | Q(is_superuser=True),
        is_active=True,
    ).distinct().order_by("first_name", "username")


class UserChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.get_full_name() or obj.username


class StudentForm(forms.ModelForm):
    mentor = UserChoiceField(queryset=get_user_model().objects.none(), required=False, label="Ментор")

    class Meta:
        model = Student
        fields = [
            "full_name", "phone", "telegram", "email", "city", "consent_pd",
            "stage", "cohort", "mentor", "price", "lost_reason",
            "partner", "utm_source", "utm_campaign", "promo_code",
            "job_company", "job_position", "job_salary", "offer_date",
            "comment",
        ]
        widgets = {
            "offer_date": DateInput(),
            "comment": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["mentor"].queryset = staff_users()
        from .permissions import is_admin
        # Ментор не может переназначать студентов и менять источник/цену
        if user is not None and not is_admin(user):
            for name in ("mentor", "partner", "price", "utm_source", "utm_campaign", "promo_code"):
                self.fields[name].disabled = True


class PaymentForm(forms.ModelForm):
    class Meta:
        model = Payment
        fields = ["amount", "due_date", "status", "paid_date", "method", "comment"]
        widgets = {"due_date": DateInput(), "paid_date": DateInput()}


class InstallmentForm(forms.Form):
    """Быстрое создание графика рассрочки: N равных платежей раз в месяц."""

    total = forms.DecimalField(label="Общая сумма", max_digits=12, decimal_places=2)
    parts = forms.IntegerField(label="Платежей", min_value=1, max_value=24, initial=3)
    first_date = forms.DateField(label="Первый платёж", widget=DateInput())


class JobApplicationForm(forms.ModelForm):
    class Meta:
        model = JobApplication
        fields = ["company", "position", "url", "status", "salary", "applied_date", "interview_at", "notes"]
        widgets = {
            "applied_date": DateInput(),
            "interview_at": DateTimeInput(),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }


class NoteForm(forms.ModelForm):
    class Meta:
        model = Note
        fields = ["text"]
        widgets = {"text": forms.Textarea(attrs={"rows": 2, "placeholder": "Созвон, фидбэк по домашке, договорённости…"})}
        labels = {"text": ""}


class PartnerPayoutForm(forms.ModelForm):
    class Meta:
        model = PartnerPayout
        fields = ["amount", "paid_date", "comment"]
        widgets = {"paid_date": DateInput()}
