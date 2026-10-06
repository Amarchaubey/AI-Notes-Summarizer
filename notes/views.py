import logging
import hashlib

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .forms import NoteUploadForm
from .models import FlashcardReview, NoteSummary, QuizAttempt, SummaryJob
from .tasks import ensure_summary_job_scheduled, schedule_summary_job
from .utils import create_study_aids, summarize_notes


logger = logging.getLogger(__name__)


def home(request):
    latest_summary = None
    study_aids = None
    processing_job = None
    form = NoteUploadForm()
    job_id = request.GET.get("job")
    if request.user.is_authenticated and job_id and job_id.isdigit():
        processing_job = SummaryJob.objects.filter(
            pk=int(job_id),
            user=request.user,
        ).first()
        if processing_job and processing_job.status in (
            SummaryJob.Status.QUEUED,
            SummaryJob.Status.RUNNING,
        ):
            ensure_summary_job_scheduled(processing_job.pk)
        if processing_job and processing_job.result_summary_id:
            latest_summary = NoteSummary.objects.prefetch_related(
                "card_reviews", "quiz_attempts"
            ).filter(
                pk=processing_job.result_summary_id,
                user=request.user,
            ).first()
    if request.user.is_authenticated and request.method == "GET":
        requested_summary = request.GET.get("summary")
        if requested_summary and requested_summary.isdigit():
            latest_summary = NoteSummary.objects.filter(
                pk=int(requested_summary),
                user=request.user,
            ).prefetch_related("card_reviews", "quiz_attempts").first()
    if request.method == "POST":
        if not request.user.is_authenticated:
            messages.info(request, "Please log in to summarize and save your notes.")
            return redirect(f"{reverse('login')}?next={reverse('home')}")
        refresh_id = request.POST.get("refresh_summary")
        confirm_job_id = request.POST.get("confirm_job")
        if refresh_id and refresh_id.isdigit():
            latest_summary = get_object_or_404(
                NoteSummary,
                pk=int(refresh_id),
                user=request.user,
            )
            try:
                summary_text = summarize_notes(
                    latest_summary.original_text,
                    length=latest_summary.summary_length,
                )
                study_aids = create_study_aids(
                    summary_text,
                    include_flowchart=bool(latest_summary.flowchart),
                )
            except (ImportError, OSError, RuntimeError, ValueError):
                logger.exception("Could not refresh a saved study summary.")
                messages.error(
                    request,
                    "This note could not be rebuilt from its source text. "
                    "Check the saved note and try again.",
                )
            else:
                latest_summary.summary_text = summary_text
                latest_summary.source_hash = hashlib.sha256(
                    latest_summary.original_text.encode("utf-8")
                ).hexdigest()
                latest_summary.flashcards = study_aids["flashcards"]
                latest_summary.quiz = study_aids["quiz"]
                latest_summary.flowchart = study_aids["flowchart"]
                latest_summary.save(
                    update_fields=[
                        "summary_text",
                        "source_hash",
                        "flashcards",
                        "quiz",
                        "flowchart",
                    ]
                )
                latest_summary.card_reviews.all().delete()
                latest_summary.quiz_attempts.all().delete()
                FlashcardReview.objects.bulk_create(
                    [
                        FlashcardReview(
                            summary=latest_summary,
                            card_index=index,
                        )
                        for index in range(len(study_aids["flashcards"]))
                    ]
                )
                messages.success(
                    request,
                    "Summary and study aids refreshed directly from the saved "
                    "chapter text. Previous quiz scores and card schedules "
                    "were reset.",
                )
        elif confirm_job_id and confirm_job_id.isdigit():
            processing_job = get_object_or_404(
                SummaryJob,
                pk=int(confirm_job_id),
                user=request.user,
                status=SummaryJob.Status.REVIEW,
            )
            form = NoteUploadForm(request.POST)
            if form.is_valid():
                processing_job.notes_text = form.cleaned_data["notes"]
                processing_job.summary_length = form.cleaned_data["summary_length"]
                processing_job.include_flowchart = form.cleaned_data["include_flowchart"]
                processing_job.source_reviewed = True
                processing_job.status = SummaryJob.Status.QUEUED
                processing_job.stage = "Ready to summarize reviewed text"
                processing_job.error = ""
                processing_job.save(
                    update_fields=[
                        "notes_text",
                        "summary_length",
                        "include_flowchart",
                        "source_reviewed",
                        "status",
                        "stage",
                        "error",
                    ]
                )
                schedule_summary_job(processing_job.pk)
                messages.success(
                    request,
                    "Your reviewed text is being summarized.",
                )
                return redirect(f"{reverse('home')}?job={processing_job.pk}")
        else:
            form = NoteUploadForm(request.POST, request.FILES)
        if not refresh_id and not confirm_job_id and form.is_valid():
            upload = form.cleaned_data.get("source_file")
            processing_job = SummaryJob.objects.create(
                user=request.user,
                notes_text=form.cleaned_data["notes"],
                upload=upload,
                upload_name=upload.name if upload else "",
                page_range=form.cleaned_data.get("page_range", ""),
                summary_length=form.cleaned_data["summary_length"],
                include_flowchart=form.cleaned_data["include_flowchart"],
            )
            schedule_summary_job(processing_job.pk)
            return redirect(f"{reverse('home')}?job={processing_job.pk}")

    saved_summaries = (
        NoteSummary.objects.filter(user=request.user).prefetch_related(
            "quiz_attempts"
        )[:10]
        if request.user.is_authenticated
        else []
    )
    due_reviews = []
    recent_attempts = []
    progress_attempts = []
    if request.user.is_authenticated:
        due_reviews = list(
            FlashcardReview.objects.filter(
                summary__user=request.user,
                due_at__lte=timezone.now(),
            ).select_related("summary")[:20]
        )
        recent_attempts = QuizAttempt.objects.filter(
            summary__user=request.user
        ).select_related("summary")[:10]
        progress_attempts = QuizAttempt.objects.filter(
            summary__user=request.user
        ).only("responses")
    topic_totals = {}
    for attempt in progress_attempts:
        for response in attempt.responses:
            topic = response["detail"]
            result = topic_totals.setdefault(topic, {"correct": 0, "missed": 0})
            result["correct" if response["correct"] else "missed"] += 1
    topic_progress = sorted(
        [
            {"topic": topic, **totals}
            for topic, totals in topic_totals.items()
        ],
        key=lambda item: (
            item["missed"] <= item["correct"],
            -item["missed"],
            -item["correct"],
        ),
    )[:8]
    return render(
        request,
        "home.html",
        {
            "form": form,
            "latest_summary": latest_summary,
            "processing_job": processing_job,
            "saved_summaries": saved_summaries,
            "due_reviews": due_reviews,
            "recent_attempts": recent_attempts,
            "topic_progress": topic_progress,
            "review_ratings": [
                (0, "Again"),
                (2, "Hard"),
                (3, "Good"),
                (5, "Easy"),
            ],
        },
    )


@login_required
def summary_job_status(request, job_id):
    job = get_object_or_404(SummaryJob, pk=job_id, user=request.user)
    if job.status in (SummaryJob.Status.QUEUED, SummaryJob.Status.RUNNING):
        ensure_summary_job_scheduled(job.pk)
        job.refresh_from_db()
    result_url = (
        f"{reverse('home')}?summary={job.result_summary_id}"
        if job.status == SummaryJob.Status.COMPLETE
        else ""
    )
    return JsonResponse(
        {
            "status": job.status,
            "stage": job.stage,
            "error": job.error,
            "result_url": result_url,
        }
    )


@login_required
def review_flashcard(request, summary_id, card_index):
    if request.method != "POST":
        return redirect("home")
    summary = get_object_or_404(NoteSummary, pk=summary_id, user=request.user)
    if card_index >= len(summary.flashcards):
        return HttpResponse(status=404)
    try:
        quality = int(request.POST.get("quality", ""))
    except ValueError:
        messages.error(request, "Choose a valid flashcard rating.")
        return redirect(f"{reverse('home')}?summary={summary.pk}")
    if quality not in range(6):
        messages.error(request, "Choose a valid flashcard rating.")
        return redirect(f"{reverse('home')}?summary={summary.pk}")

    review, _ = FlashcardReview.objects.get_or_create(
        summary=summary,
        card_index=card_index,
    )
    review.last_quality = quality
    if quality < 3:
        review.repetitions = 0
        review.interval_days = 1
    else:
        if review.repetitions == 0:
            review.interval_days = 1
        elif review.repetitions == 1:
            review.interval_days = 6
        else:
            review.interval_days = max(
                1,
                round(review.interval_days * review.ease_factor),
            )
        review.repetitions += 1
    review.ease_factor = max(
        1.3,
        review.ease_factor
        + 0.1
        - (5 - quality) * (0.08 + (5 - quality) * 0.02),
    )
    review.due_at = timezone.now() + timedelta(days=review.interval_days)
    review.save()
    messages.success(request, "Flashcard review saved.")
    return redirect(f"{reverse('home')}?summary={summary.pk}")


@login_required
def submit_quiz(request, summary_id):
    if request.method != "POST":
        return redirect("home")
    summary = get_object_or_404(NoteSummary, pk=summary_id, user=request.user)
    if not summary.quiz:
        messages.error(request, "This summary does not have a quiz yet.")
        return redirect(f"{reverse('home')}?summary={summary.pk}")
    responses = [
        {
            "detail": question["detail"],
            "correct": request.POST.get(f"question_{index}") == question["answer"],
        }
        for index, question in enumerate(summary.quiz)
    ]
    score = sum(response["correct"] for response in responses)
    attempt = QuizAttempt.objects.create(
        summary=summary,
        score=score,
        total=len(summary.quiz),
        responses=responses,
    )
    messages.success(
        request,
        f"Quiz submitted: {score} of {len(summary.quiz)} correct "
        f"({attempt.percentage}%).",
    )
    return redirect(f"{reverse('home')}?summary={summary.pk}")


def register(request):
    if request.user.is_authenticated:
        return redirect("home")
    form = UserCreationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        messages.success(request, "Your account is ready. Welcome!")
        return redirect("home")
    return render(request, "registration/register.html", {"form": form})


@login_required
def download_summary(request, summary_id):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        ListFlowable,
        ListItem,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
    )
    from xml.sax.saxutils import escape

    summary = get_object_or_404(
        NoteSummary,
        pk=summary_id,
        user=request.user,
    )
    response = HttpResponse(content_type="application/pdf")
    response["Content-Disposition"] = (
        f'attachment; filename="ai-notes-summary-{summary.pk}.pdf"'
    )

    document = SimpleDocTemplate(
        response,
        pagesize=letter,
        rightMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )
    styles = getSampleStyleSheet()
    story = [
        Paragraph(escape(summary.title), styles["Title"]),
        Spacer(1, 0.2 * inch),
        Paragraph("Summary", styles["Heading2"]),
    ]
    bullet_items = [
        ListItem(Paragraph(escape(line.lstrip("-* ").strip()), styles["BodyText"]))
        for line in summary.summary_text.splitlines()
        if line.strip()
    ]
    story.append(
        ListFlowable(
            bullet_items,
            bulletType="bullet",
            start="circle",
            bulletColor=colors.HexColor("#3457d5"),
        )
    )
    document.build(story)
    return response
