from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.text import get_valid_filename


def note_upload_path(instance, filename):
    return f"note_uploads/{instance.user_id}/{get_valid_filename(filename)}"


class NoteSummary(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="note_summaries",
    )
    title = models.CharField(max_length=120)
    original_text = models.TextField()
    summary_text = models.TextField()
    source_hash = models.CharField(max_length=64, blank=True, db_index=True)
    summary_length = models.CharField(
        max_length=10,
        choices=[
            ("short", "Short"),
            ("balanced", "Balanced"),
            ("detailed", "Detailed"),
        ],
        default="balanced",
    )
    flashcards = models.JSONField(default=list, blank=True)
    quiz = models.JSONField(default=list, blank=True)
    flowchart = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "note summaries"

    def __str__(self):
        return f"{self.title} ({self.user})"


class FlashcardReview(models.Model):
    summary = models.ForeignKey(
        NoteSummary,
        on_delete=models.CASCADE,
        related_name="card_reviews",
    )
    card_index = models.PositiveSmallIntegerField()
    due_at = models.DateTimeField(default=timezone.now)
    interval_days = models.PositiveSmallIntegerField(default=0)
    repetitions = models.PositiveSmallIntegerField(default=0)
    ease_factor = models.FloatField(default=2.5)
    last_quality = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["summary", "card_index"],
                name="unique_flashcard_review_per_summary",
            )
        ]
        ordering = ["due_at"]

    def __str__(self):
        return f"Card {self.card_index + 1}: {self.summary.title}"


class QuizAttempt(models.Model):
    summary = models.ForeignKey(
        NoteSummary,
        on_delete=models.CASCADE,
        related_name="quiz_attempts",
    )
    score = models.PositiveSmallIntegerField()
    total = models.PositiveSmallIntegerField()
    responses = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def percentage(self):
        return round(self.score * 100 / self.total) if self.total else 0


class SummaryJob(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        REVIEW = "review", "Needs review"
        COMPLETE = "complete", "Complete"
        FAILED = "failed", "Failed"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="summary_jobs",
    )
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.QUEUED,
        db_index=True,
    )
    stage = models.CharField(max_length=120, default="Waiting to start")
    error = models.TextField(blank=True)
    notes_text = models.TextField(blank=True)
    upload = models.FileField(upload_to=note_upload_path, blank=True)
    upload_name = models.CharField(max_length=255, blank=True)
    page_range = models.CharField(max_length=32, blank=True)
    summary_length = models.CharField(
        max_length=10,
        choices=NoteSummary._meta.get_field("summary_length").choices,
        default="balanced",
    )
    include_flowchart = models.BooleanField(default=False)
    source_reviewed = models.BooleanField(default=False)
    result_summary = models.ForeignKey(
        NoteSummary,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processing_jobs",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Summary job {self.pk} ({self.get_status_display()})"
