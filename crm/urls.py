from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("board/", views.board, name="board"),
    path("board/move/", views.board_move, name="board_move"),
    path("students/", views.student_list, name="student_list"),
    path("students/new/", views.student_create, name="student_create"),
    path("students/<int:pk>/", views.student_detail, name="student_detail"),
    path("students/<int:pk>/edit/", views.student_edit, name="student_edit"),
    path("students/<int:pk>/stage/", views.student_stage, name="student_stage"),
    path("students/<int:pk>/notes/", views.note_add, name="note_add"),
    path("students/<int:pk>/payments/", views.payment_add, name="payment_add"),
    path("students/<int:pk>/installments/", views.installments_add, name="installments_add"),
    path("students/<int:pk>/applications/", views.application_add, name="application_add"),
    path("payments/", views.payments, name="payments"),
    path("payments/<int:pk>/edit/", views.payment_edit, name="payment_edit"),
    path("payments/<int:pk>/paid/", views.payment_paid, name="payment_paid"),
    path("applications/<int:pk>/edit/", views.application_edit, name="application_edit"),
    path("partners/", views.partner_list, name="partner_list"),
    path("partners/<int:pk>/", views.partner_report, name="partner_report"),
    path("partners/<int:pk>/payouts/", views.payout_add, name="payout_add"),
    path("backup/import/", views.backup_import, name="backup_import"),
    path("api/leads/", views.api_lead, name="api_lead"),
]
