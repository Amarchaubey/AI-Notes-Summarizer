import hashlib
import os
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from reportlab.pdfgen import canvas

from .forms import NoteUploadForm
from .models import FlashcardReview, NoteSummary, QuizAttempt, SummaryJob
from .utils import create_study_aids
from .utils import summarize_notes
from .tasks import ensure_summary_job_scheduled, process_summary_job


def make_pdf(text=None):
    buffer = BytesIO()
    document = canvas.Canvas(buffer)
    if text:
        document.drawString(72, 720, text)
    document.showPage()
    document.save()
    return buffer.getvalue()


def complete_summary_job(job_id):
    process_summary_job(job_id)
    job = SummaryJob.objects.get(pk=job_id)
    if job.status == SummaryJob.Status.REVIEW:
        job.status = SummaryJob.Status.QUEUED
        job.source_reviewed = True
        job.save(update_fields=["status", "source_reviewed"])
        process_summary_job(job_id)


class NotesApplicationTests(TestCase):
    def test_health_check_reports_database_connectivity(self):
        response = self.client.get(reverse("health_check"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def setUp(self):
        self.user = User.objects.create_user(username="reader", password="safe-password")

    def test_register_creates_and_logs_in_user(self):
        response = self.client.post(
            reverse("register"),
            {
                "username": "new-reader",
                "password1": "Another-safe-pass-824!",
                "password2": "Another-safe-pass-824!",
            },
        )
        self.assertRedirects(response, reverse("home"))
        self.assertTrue(User.objects.filter(username="new-reader").exists())

    @patch(
        "notes.tasks.summarize_notes",
        return_value=(
            "- [Learning] Active recall is retrieving information from memory.\n"
            "- [Learning] Spaced practice is reviewing material over time."
        ),
    )
    def test_authenticated_user_can_summarize_and_save_notes(self, summarize):
        self.client.force_login(self.user)
        with patch(
            "notes.views.schedule_summary_job",
            side_effect=process_summary_job,
        ):
            response = self.client.post(
                reverse("home"),
                {"notes": "A long page of notes."},
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Active recall")
        saved = NoteSummary.objects.get(user=self.user)
        self.assertEqual(saved.original_text, "A long page of notes.")
        self.assertEqual(len(saved.flashcards), 2)
        self.assertEqual(len(saved.quiz), 2)
        self.assertEqual(saved.flowchart, [])
        self.assertEqual(saved.card_reviews.count(), 2)
        summarize.assert_called_once_with(
            "A long page of notes.", length="balanced"
        )

    @patch(
        "notes.tasks.summarize_notes",
        return_value=(
            "- Gather ingredients before cooking.\n"
            "- Mix ingredients and bake the mixture."
        ),
    )
    def test_flowchart_can_be_requested_and_is_saved(self, summarize):
        self.client.force_login(self.user)
        with patch(
            "notes.views.schedule_summary_job",
            side_effect=process_summary_job,
        ):
            response = self.client.post(
                reverse("home"),
                {"notes": "How to bake a cake.", "include_flowchart": "on"},
                follow=True,
            )
        saved = NoteSummary.objects.get(user=self.user)
        self.assertEqual(len(saved.flowchart), 2)
        self.assertContains(response, "Flowchart")
        self.assertEqual(
            [step["type"] for step in saved.flowchart],
            ["step", "point"],
        )
        summarize.assert_called_once()

    def test_text_file_upload_is_used_as_notes(self):
        self.client.force_login(self.user)
        with patch("notes.views.schedule_summary_job") as schedule:
            response = self.client.post(
                reverse("home"),
                {
                    "notes": "",
                    "source_file": SimpleUploadedFile(
                        "class-notes.txt", b"Uploaded class notes."
                    ),
                },
            )
        self.assertEqual(response.status_code, 302)
        job = SummaryJob.objects.get(user=self.user)
        self.assertEqual(job.upload_name, "class-notes.txt")
        schedule.assert_called_once_with(job.pk)

    @patch("notes.tasks.summarize_notes", return_value="- Uploaded takeaway.")
    def test_background_job_creates_summary_and_removes_temporary_upload(self, summarize):
        job = SummaryJob.objects.create(
            user=self.user,
            upload=SimpleUploadedFile("class-notes.txt", b"Uploaded class notes."),
            upload_name="class-notes.txt",
        )
        upload_path = job.upload.path
        process_summary_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, SummaryJob.Status.REVIEW)
        self.assertEqual(job.notes_text, "Uploaded class notes.")
        self.assertFalse(job.upload)
        self.assertFalse(os.path.exists(upload_path))

        job.status = SummaryJob.Status.QUEUED
        job.source_reviewed = True
        job.save(update_fields=["status", "source_reviewed"])
        process_summary_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, SummaryJob.Status.COMPLETE)
        self.assertEqual(job.result_summary.original_text, "Uploaded class notes.")
        summarize.assert_called_once_with(
            "Uploaded class notes.", length="balanced"
        )

    @patch(
        "notes.tasks.summarize_notes",
        return_value="- [C Programming] A pointer is a variable that stores a memory address.",
    )
    def test_uploaded_text_can_be_corrected_before_summary(self, summarize):
        self.client.force_login(self.user)
        with patch(
            "notes.views.schedule_summary_job",
            side_effect=process_summary_job,
        ):
            preview_response = self.client.post(
                reverse("home"),
                {
                    "notes": "",
                    "source_file": SimpleUploadedFile(
                        "pointers.txt",
                        b"A pointer is a varaible that stores a memory address.",
                    ),
                },
                follow=True,
            )
        self.assertEqual(preview_response.status_code, 200)
        self.assertContains(preview_response, "Review the extracted text")
        self.assertContains(preview_response, "varaible")
        job = SummaryJob.objects.get(user=self.user)
        self.assertEqual(job.status, SummaryJob.Status.REVIEW)

        with patch(
            "notes.views.schedule_summary_job",
            side_effect=complete_summary_job,
        ):
            result_response = self.client.post(
                reverse("home"),
                {
                    "confirm_job": str(job.pk),
                    "notes": "A pointer is a variable that stores a memory address.",
                    "summary_length": "detailed",
                },
                follow=True,
            )
        job.refresh_from_db()
        self.assertEqual(job.status, SummaryJob.Status.COMPLETE)
        self.assertEqual(
            job.result_summary.original_text,
            "A pointer is a variable that stores a memory address.",
        )
        self.assertEqual(job.result_summary.summary_length, "detailed")
        self.assertContains(result_response, "A pointer is a variable")
        summarize.assert_called_once_with(
            "A pointer is a variable that stores a memory address.",
            length="detailed",
        )

    @patch("notes.tasks.summarize_notes", return_value="- Saved takeaway.")
    def test_identical_user_notes_reuse_saved_summary_without_regeneration(self, summarize):
        text = "Identical study note content."
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        existing = NoteSummary.objects.create(
            user=self.user,
            title="First copy",
            original_text=text,
            summary_text="- Saved takeaway.",
            source_hash=digest,
            summary_length="balanced",
        )
        job = SummaryJob.objects.create(
            user=self.user,
            notes_text=text,
            summary_length="balanced",
        )
        process_summary_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.result_summary_id, existing.pk)
        self.assertEqual(NoteSummary.objects.filter(user=self.user).count(), 1)
        summarize.assert_not_called()

    def test_identical_notes_are_cached_per_user_only(self):
        text = "Identical study note content."
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        other = User.objects.create_user(username="other-cacher", password="safe-password")
        NoteSummary.objects.create(
            user=other,
            title="Private copy",
            original_text=text,
            summary_text="- Private takeaway.",
            source_hash=digest,
        )
        self.assertFalse(
            NoteSummary.objects.filter(
                user=self.user,
                source_hash=digest,
                original_text=text,
            ).exists()
        )

    def test_pdf_page_range_is_stored_and_invalid_ranges_are_rejected(self):
        valid = NoteUploadForm(
            data={"notes": "", "page_range": "3-8"},
            files={
                "source_file": SimpleUploadedFile(
                    "pages.pdf", make_pdf("Page content."), content_type="application/pdf"
                )
            },
        )
        self.assertTrue(valid.is_valid(), valid.errors)
        self.client.force_login(self.user)
        with patch("notes.views.schedule_summary_job") as schedule:
            self.client.post(
                reverse("home"),
                {
                    "notes": "",
                    "page_range": "3-8",
                    "source_file": SimpleUploadedFile(
                        "pages.pdf", make_pdf("Page content."), content_type="application/pdf"
                    ),
                },
            )
        self.assertEqual(SummaryJob.objects.get(user=self.user).page_range, "3-8")
        schedule.assert_called_once()

        invalid = NoteUploadForm(
            data={"notes": "", "page_range": "8-3"},
            files={
                "source_file": SimpleUploadedFile(
                    "pages.pdf", make_pdf("Page content."), content_type="application/pdf"
                )
            },
        )
        self.assertFalse(invalid.is_valid())
        self.assertIn("Page ranges must start at 1", str(invalid.errors))

    def test_review_submission_rejects_empty_or_oversized_corrections(self):
        job = SummaryJob.objects.create(
            user=self.user,
            status=SummaryJob.Status.REVIEW,
            notes_text="Extracted text.",
            source_reviewed=False,
            upload_name="chapter.txt",
        )
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("home"),
            {"confirm_job": str(job.pk), "notes": "   "},
        )
        self.assertEqual(response.status_code, 200)
        job.refresh_from_db()
        self.assertEqual(job.status, SummaryJob.Status.REVIEW)
        self.assertEqual(NoteSummary.objects.filter(user=self.user).count(), 0)

    def test_job_status_is_private_to_authenticated_owner(self):
        job = SummaryJob.objects.create(user=self.user, notes_text="Private text")
        other = User.objects.create_user(username="job-viewer", password="safe-password")
        self.client.force_login(other)
        response = self.client.get(reverse("summary_job_status", args=[job.pk]))
        self.assertEqual(response.status_code, 404)

    def test_job_status_endpoint_schedules_and_reports_progress(self):
        job = SummaryJob.objects.create(user=self.user, notes_text="Queued study text.")
        self.client.force_login(self.user)
        with patch("notes.views.ensure_summary_job_scheduled") as schedule:
            response = self.client.get(
                reverse("summary_job_status", args=[job.pk])
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], SummaryJob.Status.QUEUED)
        self.assertEqual(response.json()["stage"], "Waiting to start")
        schedule.assert_called_once_with(job.pk)

    def test_interrupted_running_job_is_requeued_for_resume(self):
        job = SummaryJob.objects.create(
            user=self.user,
            notes_text="Resume this note.",
            status=SummaryJob.Status.RUNNING,
            stage="Reading notes",
        )
        with patch("notes.tasks.schedule_summary_job") as schedule:
            ensure_summary_job_scheduled(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, SummaryJob.Status.QUEUED)
        self.assertEqual(job.stage, "Resuming after a server restart")
        schedule.assert_called_once_with(job.pk)

    @patch("notes.tasks.summarize_notes", return_value="- PDF takeaway.")
    def test_text_pdf_upload_is_summarized_and_saved(self, summarize):
        self.client.force_login(self.user)
        with patch(
            "notes.views.schedule_summary_job",
            side_effect=complete_summary_job,
        ):
            response = self.client.post(
                reverse("home"),
                {
                    "notes": "",
                    "source_file": SimpleUploadedFile(
                        "lecture.pdf",
                        make_pdf("Selectable PDF text for summarization."),
                        content_type="application/pdf",
                    ),
                },
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "PDF takeaway.")
        saved = NoteSummary.objects.get(user=self.user)
        self.assertEqual(saved.original_text, "Selectable PDF text for summarization.")
        summarize.assert_called_once_with(
            "Selectable PDF text for summarization.", length="balanced"
        )

    @patch("notes.tasks.extract_pdf_text", side_effect=ValueError("No readable text was found."))
    def test_image_only_pdf_failure_is_reported_by_background_job(self, extract):
        self.client.force_login(self.user)
        with (
            patch("notes.views.schedule_summary_job", side_effect=process_summary_job),
            patch("notes.tasks.logger.exception"),
        ):
            response = self.client.post(
                reverse("home"),
                {
                    "notes": "",
                    "source_file": SimpleUploadedFile(
                        "scan.pdf", make_pdf(), content_type="application/pdf"
                    ),
                },
                follow=True,
            )
        self.assertContains(response, "No readable text was found.")
        self.assertEqual(SummaryJob.objects.get(user=self.user).status, SummaryJob.Status.FAILED)
        extract.assert_called_once()

    def test_page_range_is_validated_without_reading_pdf_in_request(self):
        form = NoteUploadForm(
            data={"notes": "", "page_range": "3-8"},
            files={
                "source_file": SimpleUploadedFile(
                    "pages.pdf", make_pdf("Page text."), content_type="application/pdf"
                )
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["page_range"], "3-8")

    def test_pdf_upload_over_10_mb_is_rejected(self):
        form = NoteUploadForm(
            data={"notes": ""},
            files={
                "source_file": SimpleUploadedFile(
                    "oversized.pdf",
                    b"%PDF-" + b"0" * (10 * 1024 * 1024),
                    content_type="application/pdf",
                )
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("PDF must be 10 MB or smaller", str(form.non_field_errors()))

    def test_pdf_download_is_limited_to_the_owner(self):
        other_user = User.objects.create_user(
            username="another-reader", password="safe-password"
        )
        saved = NoteSummary.objects.create(
            user=other_user,
            title="Private notes",
            original_text="Original",
            summary_text="- Private takeaway.",
        )
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("download_summary", args=[saved.pk])
        )
        self.assertEqual(response.status_code, 404)

    @patch("notes.tasks.summarize_notes", return_value="- Quick short summary.")
    def test_summary_length_is_selected_and_saved(self, summarize):
        self.client.force_login(self.user)
        with patch(
            "notes.views.schedule_summary_job",
            side_effect=process_summary_job,
        ):
            self.client.post(
                reverse("home"),
                {"notes": "A long page of notes.", "summary_length": "short"},
            )
        saved = NoteSummary.objects.get(user=self.user)
        self.assertEqual(saved.summary_length, "short")
        summarize.assert_called_once_with(
            "A long page of notes.", length="short"
        )

    def test_quiz_attempt_is_saved_and_score_is_server_calculated(self):
        summary = NoteSummary.objects.create(
            user=self.user,
            title="Recall",
            original_text="Notes",
            summary_text="- [Learning] Active recall is retrieving information from memory.\n"
            "- [Learning] Spaced practice is reviewing material over time.",
            quiz=create_study_aids(
                "- [Learning] Active recall is retrieving information from memory.\n"
                "- [Learning] Spaced practice is reviewing material over time."
            )["quiz"],
        )
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("submit_quiz", args=[summary.pk]),
            {
                "question_0": summary.quiz[0]["answer"],
                "question_1": "Not the answer",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Best score:")
        self.assertContains(response, "50%")
        self.assertContains(response, "Topics to revisit")
        attempt = QuizAttempt.objects.get(summary=summary)
        self.assertEqual((attempt.score, attempt.total, attempt.percentage), (1, 2, 50))
        self.assertEqual(
            sum(item["correct"] for item in attempt.responses),
            1,
        )
        self.assertEqual(len(attempt.responses), 2)

    def test_flashcard_review_is_spaced_and_scoped_to_the_owner(self):
        summary = NoteSummary.objects.create(
            user=self.user,
            title="Recall",
            original_text="Notes",
            summary_text="- [Learning] Active recall is retrieving information from memory.",
            flashcards=create_study_aids(
                "- [Learning] Active recall is retrieving information from memory."
            )["flashcards"],
        )
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("review_flashcard", args=[summary.pk, 0]), {"quality": "5"}
        )
        self.assertRedirects(response, f"{reverse('home')}?summary={summary.pk}")
        review = FlashcardReview.objects.get(summary=summary, card_index=0)
        self.assertEqual(review.interval_days, 1)
        self.assertEqual(review.repetitions, 1)
        self.assertGreater(review.due_at, timezone.now())

        other_user = User.objects.create_user(
            username="other", password="safe-password"
        )
        self.client.force_login(other_user)
        response = self.client.post(
            reverse("review_flashcard", args=[summary.pk, 0]), {"quality": "5"}
        )
        self.assertEqual(response.status_code, 404)

    def test_flashcard_schedule_grows_when_recalled_and_resets_when_missed(self):
        summary = NoteSummary.objects.create(
            user=self.user,
            title="Recall",
            original_text="Notes",
            summary_text="- [Learning] Active recall is retrieving information from memory.",
            flashcards=create_study_aids(
                "- [Learning] Active recall is retrieving information from memory."
            )["flashcards"],
        )
        self.client.force_login(self.user)
        url = reverse("review_flashcard", args=[summary.pk, 0])
        for quality in ("5", "5"):
            self.client.post(url, {"quality": quality})
        review = FlashcardReview.objects.get(summary=summary, card_index=0)
        self.assertEqual((review.interval_days, review.repetitions), (6, 2))

        self.client.post(url, {"quality": "0"})
        review.refresh_from_db()
        self.assertEqual((review.interval_days, review.repetitions), (1, 0))

    def test_quiz_submission_cannot_record_results_for_another_user(self):
        private_summary = NoteSummary.objects.create(
            user=User.objects.create_user(
                username="other", password="safe-password"
            ),
            title="Private quiz",
            original_text="Notes",
            summary_text="Summary",
            quiz=[
                {
                    "prompt": "Question?",
                    "answer": "Answer",
                    "options": ["Answer", "Wrong"],
                }
            ],
        )
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("submit_quiz", args=[private_summary.pk]),
            {"question_0": "Answer"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(QuizAttempt.objects.count(), 0)

    def test_owner_can_download_summary_as_pdf(self):
        saved = NoteSummary.objects.create(
            user=self.user,
            title="My class notes",
            original_text="Original notes",
            summary_text="- Key takeaway.",
        )
        self.client.force_login(self.user)
        response = self.client.get(reverse("download_summary", args=[saved.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF-"))


class SummarizationUtilityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="utility-reader",
            password="safe-password",
        )

    @patch("pymupdf.open")
    def test_scanned_pdf_text_is_extracted_with_ocr(self, open_pdf):
        from unittest.mock import MagicMock

        from .utils import extract_pdf_text

        page = MagicMock()
        page.get_text.side_effect = ["", "Recognized words from scan."]
        page.get_textpage_ocr.return_value = object()
        document = MagicMock()
        document.__enter__.return_value = document
        document.is_encrypted = False
        document.page_count = 1
        document.__iter__.return_value = iter([page])
        document.pages.return_value = [page]
        open_pdf.return_value = document

        self.assertEqual(
            extract_pdf_text(b"pdf-bytes"),
            "Recognized words from scan.",
        )
        page.get_textpage_ocr.assert_called_once()
        self.assertEqual(
            page.get_textpage_ocr.call_args.kwargs["language"],
            "eng",
        )
        self.assertTrue(
            page.get_textpage_ocr.call_args.kwargs["tessdata"].endswith("tessdata")
        )

    @patch("pymupdf.open")
    def test_pdf_extraction_reads_only_the_requested_page_range(self, open_pdf):
        from unittest.mock import MagicMock

        from .utils import extract_pdf_text

        page = MagicMock()
        page.get_text.return_value = "Selected chapter page content."
        document = MagicMock()
        document.__enter__.return_value = document
        document.is_encrypted = False
        document.page_count = 12
        document.pages.return_value = [page]
        open_pdf.return_value = document
        self.assertEqual(
            extract_pdf_text(b"pdf-bytes", page_range="3-4"),
            "Selected chapter page content.",
        )
        document.pages.assert_called_once_with(2, 4)

    def test_study_aids_create_recall_cards_quiz_and_optional_flowchart(self):
        from .utils import create_study_aids

        summary = (
            "- An algorithm is a finite sequence of clear steps to solve a problem; first, define its inputs.\n"
            "- A flowchart is a visual representation of an algorithm; if a decision occurs, show both branches."
        )
        study_aids = create_study_aids(
            summary,
            include_flowchart=True,
        )
        self.assertEqual(len(study_aids["flashcards"]), 2)
        self.assertEqual(len(study_aids["quiz"]), 2)
        self.assertEqual(
            [step["type"] for step in study_aids["flowchart"]],
            ["step", "decision"],
        )
        for question in study_aids["quiz"]:
            self.assertIn(question["answer"], question["options"])

    def test_quiz_is_omitted_when_there_is_only_one_recall_point(self):
        from .utils import create_study_aids

        study_aids = create_study_aids("- Practice improves retention.")
        self.assertEqual(len(study_aids["flashcards"]), 1)
        self.assertEqual(study_aids["quiz"], [])

    def test_summary_uses_chapter_content_not_table_of_contents_or_headers(self):
        from .utils import summarize_notes

        notes = """PPS 1st Year — B.Tech Exam Notes
Page 1
Table of Contents
• 1. Problem Solving Fundamentals
• 2. Algorithms & Flowcharts
PPS 1st Year — B.Tech Exam Notes
Page 2
1. Problem Solving Fundamentals
What is problem solving?
Problem solving is the systematic process of understanding a problem, designing a solution,
implementing it, testing it, and maintaining it.
2. Algorithms & Flowcharts
What is an algorithm?
An algorithm is a finite sequence of clear instructions to solve a problem.
"""
        summary = summarize_notes(notes, length="balanced")
        self.assertNotIn("Table of Contents", summary)
        self.assertNotIn("PPS 1st Year", summary)
        self.assertIn("Problem solving is the systematic process", summary)
        self.assertIn("An algorithm is a finite sequence", summary)
        self.assertTrue(all(line.startswith("- [") for line in summary.splitlines()))

    def test_summary_length_sets_source_point_count(self):
        from .utils import summarize_notes

        notes = "\n".join(
            f"{index}. Chapter {index}\n"
            f"Concept {index} is the important principle used to solve problem {index} reliably."
            for index in range(1, 13)
        )
        short = summarize_notes(notes, length="short")
        detailed = summarize_notes(notes, length="detailed")
        self.assertLessEqual(len(short.splitlines()), 10)
        self.assertGreater(len(detailed.splitlines()), len(short.splitlines()))
        with self.assertRaises(ValueError):
            summarize_notes(notes, length="unknown")

    def test_definition_flashcard_explains_concept_and_quiz_uses_source_options(self):
        summary = (
            "- [C Programming] A pointer is a variable that stores a memory address.\n"
            "- [C Programming] An array is a collection of elements of the same type."
        )
        aids = create_study_aids(summary)
        pointer_card = aids["flashcards"][0]
        self.assertIn("what is pointer", pointer_card["prompt"])
        self.assertIn("stores a memory address", pointer_card["answer"])
        self.assertEqual(len(aids["quiz"]), 2)
        self.assertEqual(
            {option for question in aids["quiz"] for option in question["options"]},
            {"pointer", "array"},
        )

    def test_saved_summary_can_be_rebuilt_from_original_notes(self):
        original_text = (
            "1. Algorithms\n"
            "An algorithm is a finite sequence of precise instructions for solving a problem.\n"
            "2. Flowcharts\n"
            "A flowchart is a graphical representation of an algorithm using standard symbols."
        )
        saved = NoteSummary.objects.create(
            user=self.user,
            title="Old generic summary",
            original_text=original_text,
            summary_text="- Generic generated fragment.",
            flashcards=[{"prompt": "old", "answer": "old"}],
            quiz=[{"prompt": "old", "answer": "old", "options": ["old"]}],
            flowchart=["old"],
        )
        FlashcardReview.objects.create(summary=saved, card_index=0)
        QuizAttempt.objects.create(summary=saved, score=0, total=1)
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("home"),
            {"refresh_summary": str(saved.pk)},
            follow=True,
        )
        saved.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Summary and study aids refreshed")
        self.assertIn("An algorithm is a finite sequence", saved.summary_text)
        self.assertNotIn("Generic generated fragment", saved.summary_text)
        self.assertEqual(saved.card_reviews.count(), 2)
        self.assertEqual(saved.quiz_attempts.count(), 0)
