from django.contrib import admin

from .models import NoteSummary


@admin.register(NoteSummary)
class NoteSummaryAdmin(admin.ModelAdmin):
    list_display = ("title", "user", "created_at")
    list_filter = ("created_at",)
    search_fields = ("title", "user__username")
