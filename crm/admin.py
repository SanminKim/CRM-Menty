from django.contrib import admin

from .models import (
    Cohort, JobApplication, Meeting, Note, Partner, PartnerPayout, Payment, StageHistory, Student,
)

admin.site.site_header = "CRM менторства — администрирование"
admin.site.site_title = "CRM менторства"
admin.site.index_title = "Справочники и данные"


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0


class JobApplicationInline(admin.TabularInline):
    model = JobApplication
    extra = 0
    fields = ("company", "position", "status", "salary", "applied_date")


class NoteInline(admin.TabularInline):
    model = Note
    extra = 0
    fields = ("text", "author", "created_at")
    readonly_fields = ("created_at",)


class StageHistoryInline(admin.TabularInline):
    model = StageHistory
    extra = 0
    can_delete = False
    readonly_fields = ("from_stage", "to_stage", "changed_by", "changed_at")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Student)
class StudentAdmin(admin.ModelAdmin):
    list_display = ("full_name", "stage", "cohort", "mentor", "partner", "price", "created_at")
    list_filter = ("stage", "cohort", "mentor", "partner")
    search_fields = ("full_name", "phone", "email", "telegram", "promo_code")
    date_hierarchy = "created_at"
    inlines = [PaymentInline, JobApplicationInline, NoteInline, StageHistoryInline]

    def save_model(self, request, obj, form, change):
        obj.save(changed_by=request.user)


@admin.register(Partner)
class PartnerAdmin(admin.ModelAdmin):
    list_display = ("name", "promo_code", "utm_source", "share_percent", "is_active")


class PartnerPayoutInline(admin.TabularInline):
    model = PartnerPayout
    extra = 0


PartnerAdmin.inlines = [PartnerPayoutInline]


@admin.register(Cohort)
class CohortAdmin(admin.ModelAdmin):
    list_display = ("name", "start_date", "end_date", "price", "is_active")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("student", "amount", "due_date", "paid_date", "status", "method")
    list_filter = ("status",)
    search_fields = ("student__full_name",)
    date_hierarchy = "due_date"


@admin.register(PartnerPayout)
class PartnerPayoutAdmin(admin.ModelAdmin):
    list_display = ("partner", "amount", "paid_date", "comment")


@admin.register(JobApplication)
class JobApplicationAdmin(admin.ModelAdmin):
    list_display = ("student", "company", "position", "status", "salary", "applied_date")
    list_filter = ("status",)
    search_fields = ("student__full_name", "company")


@admin.register(StageHistory)
class StageHistoryAdmin(admin.ModelAdmin):
    list_display = ("student", "from_stage", "to_stage", "changed_by", "changed_at")
    list_filter = ("to_stage",)


@admin.register(Meeting)
class MeetingAdmin(admin.ModelAdmin):
    list_display = ("date", "time", "title", "kind", "status", "student", "cohort", "mentor")
    list_filter = ("status", "kind", "mentor")
    search_fields = ("title", "student__full_name")
    date_hierarchy = "date"
    autocomplete_fields = ("student",)
