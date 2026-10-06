"""Persistent summary job processing for the local Django application."""

import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from .models import FlashcardReview, NoteSummary, SummaryJob
from .utils import create_study_aids, extract_pdf_text, summarize_notes


logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="note-summary")
_in_flight = set()
_in_flight_lock = Lock()


def schedule_summary_job(job_id):
    with _in_flight_lock:
        if job_id in _in_flight:
            return
        _in_flight.add(job_id)
    future = _executor.submit(process_summary_job, job_id)
    future.add_done_callback(lambda _: _discard_job(job_id))


def _discard_job(job_id):
    with _in_flight_lock:
        _in_flight.discard(job_id)


def _is_in_flight(job_id):
    with _in_flight_lock:
        return job_id in _in_flight


def ensure_summary_job_scheduled(job_id):
    job = SummaryJob.objects.get(pk=job_id)
    if job.status == SummaryJob.Status.RUNNING and not _is_in_flight(job_id):
        SummaryJob.objects.filter(
            pk=job_id,
            status=SummaryJob.Status.RUNNING,
        ).update(
            status=SummaryJob.Status.QUEUED,
            stage="Resuming after a server restart",
        )
        job.refresh_from_db()
    if job.status == SummaryJob.Status.QUEUED:
        schedule_summary_job(job_id)


def _read_job_notes(job):
    if not job.upload:
        return job.notes_text.strip()
    with job.upload.open("rb") as uploaded_file:
        content = uploaded_file.read()
    if job.upload_name.lower().endswith(".pdf"):
        return extract_pdf_text(content, page_range=job.page_range)
    try:
        text = content.decode("utf-8-sig").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("Text files must use UTF-8 encoding.") from exc
    if not text:
        raise ValueError("The uploaded text file is empty.")
    if len(text) > settings.NOTES_MAX_CHARACTERS:
        raise ValueError(
            f"Extracted notes must be {settings.NOTES_MAX_CHARACTERS:,} "
            "characters or fewer."
        )
    return text


def process_summary_job(job_id):
    close_old_connections()
    upload_name = ""
    upload_name_to_delete = ""
    try:
        with transaction.atomic():
            job = SummaryJob.objects.select_for_update().get(pk=job_id)
            if job.status != SummaryJob.Status.QUEUED:
                return
            job.status = SummaryJob.Status.RUNNING
            job.started_at = timezone.now()
            job.stage = (
                "Extracting selected PDF pages and OCR"
                if job.upload_name.lower().endswith(".pdf")
                else "Reading notes"
            )
            job.save(update_fields=["status", "started_at", "stage"])
            upload_name = job.upload.name if job.upload else ""
            upload_name_to_delete = job.upload.path if job.upload else ""

        job = SummaryJob.objects.get(pk=job_id)
        notes = _read_job_notes(job)
        if not notes:
            raise ValueError("No readable notes were found in this submission.")
        if len(notes) > settings.NOTES_MAX_CHARACTERS:
            raise ValueError(
                f"Extracted notes must be {settings.NOTES_MAX_CHARACTERS:,} "
                "characters or fewer."
            )

        if job.upload_name and not job.source_reviewed:
            SummaryJob.objects.filter(pk=job_id).update(
                status=SummaryJob.Status.REVIEW,
                stage="Review the extracted text before summarizing",
                notes_text=notes,
                upload="",
                error="",
            )
            return

        source_hash = hashlib.sha256(notes.encode("utf-8")).hexdigest()
        SummaryJob.objects.filter(pk=job_id).update(
            stage="Checking for a saved copy of these notes"
        )
        cached = NoteSummary.objects.filter(
            user=job.user,
            source_hash=source_hash,
            summary_length=job.summary_length,
            original_text=notes,
        ).order_by("-created_at").first()
        if cached and bool(cached.flowchart) == job.include_flowchart:
            summary = cached
            SummaryJob.objects.filter(pk=job_id).update(
                stage="Reusing your saved summary"
            )
        else:
            SummaryJob.objects.filter(pk=job_id).update(
                stage="Selecting important statements from your notes"
            )
            summary_text = summarize_notes(notes, length=job.summary_length)
            aids = create_study_aids(
                summary_text,
                include_flowchart=job.include_flowchart,
            )
            first_line = next(
                (
                    line.strip()
                    for line in notes.splitlines()
                    if line.strip()
                    and not line.strip().lower().startswith("page ")
                ),
                "Untitled notes",
            )
            if job.upload_name:
                file_stem = Path(job.upload_name).stem.replace("_", " ").strip()
                if file_stem:
                    first_line = file_stem
            summary = NoteSummary.objects.create(
                user=job.user,
                title=first_line[:117] + "..." if len(first_line) > 120 else first_line,
                original_text=notes,
                source_hash=source_hash,
                summary_text=summary_text,
                summary_length=job.summary_length,
                flashcards=aids["flashcards"],
                quiz=aids["quiz"],
                flowchart=aids["flowchart"],
            )
            FlashcardReview.objects.bulk_create(
                [
                    FlashcardReview(summary=summary, card_index=index)
                    for index in range(len(aids["flashcards"]))
                ]
            )

        SummaryJob.objects.filter(pk=job_id).update(
            status=SummaryJob.Status.COMPLETE,
            stage="Summary ready",
            result_summary=summary,
            completed_at=timezone.now(),
            notes_text="",
            error="",
            upload="",
        )
    except SummaryJob.DoesNotExist:
        logger.warning("Summary job %s disappeared before processing.", job_id)
    except Exception as exc:
        logger.exception("Summary job %s failed.", job_id)
        SummaryJob.objects.filter(pk=job_id).update(
            status=SummaryJob.Status.FAILED,
            stage="Could not process these notes",
            error=str(exc) if isinstance(exc, ValueError) else (
                "The job failed unexpectedly. Check the server log and try again."
            ),
            completed_at=timezone.now(),
            upload="",
        )
    finally:
        if upload_name_to_delete:
            try:
                Path(upload_name_to_delete).unlink(missing_ok=True)
            except OSError:
                logger.exception("Could not remove uploaded file %s.", upload_name)
        close_old_connections()
