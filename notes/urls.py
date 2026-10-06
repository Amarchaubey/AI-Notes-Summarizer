from django.db import DatabaseError, connection
from django.contrib.auth import views as auth_views
from django.http import JsonResponse
from django.urls import path

from . import views


def health_check(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("healthz/", health_check, name="health_check"),
    path("", views.home, name="home"),
    path(
        "jobs/<int:job_id>/status/",
        views.summary_job_status,
        name="summary_job_status",
    ),
    path("register/", views.register, name="register"),
    path(
        "login/",
        auth_views.LoginView.as_view(template_name="registration/login.html"),
        name="login",
    ),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path(
        "summary/<int:summary_id>/review/<int:card_index>/",
        views.review_flashcard,
        name="review_flashcard",
    ),
    path(
        "summary/<int:summary_id>/quiz/",
        views.submit_quiz,
        name="submit_quiz",
    ),
    path("summary/<int:summary_id>/pdf/", views.download_summary, name="download_summary"),
]
