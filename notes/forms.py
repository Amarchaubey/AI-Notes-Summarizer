import re

from django import forms
from django.conf import settings


class NoteUploadForm(forms.Form):
    summary_length = forms.ChoiceField(
        label="Summary detail",
        choices=[
            ("short", "Short"),
            ("balanced", "Balanced"),
            ("detailed", "Detailed"),
        ],
        initial="balanced",
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    include_flowchart = forms.BooleanField(
        required=False,
        label="Also create a flowchart from the summary points",
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    page_range = forms.CharField(
        label="PDF pages (optional)",
        required=False,
        max_length=16,
        help_text="For example, 3-8. Leave blank to use every page.",
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": "All pages",
                "inputmode": "numeric",
                "pattern": r"\d+(?:-\d+)?",
            }
        ),
    )
    notes = forms.CharField(
        label="Your notes",
        required=False,
        max_length=settings.NOTES_MAX_CHARACTERS,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 10,
                "placeholder": "Paste your notes here...",
            }
        ),
    )
    source_file = forms.FileField(
        label="Or upload a .txt or .pdf file",
        required=False,
        widget=forms.ClearableFileInput(
            attrs={
                "class": "form-control",
                "accept": ".txt,.pdf,text/plain,application/pdf",
            }
        ),
    )

    def clean_summary_length(self):
        return self.cleaned_data.get("summary_length") or "balanced"

    def clean(self):
        cleaned_data = super().clean()
        notes = (cleaned_data.get("notes") or "").strip()
        source_file = cleaned_data.get("source_file")

        if notes and source_file:
            raise forms.ValidationError("Paste notes or upload a file, not both.")

        page_range = (cleaned_data.get("page_range") or "").strip()
        match = None
        if page_range:
            match = re.fullmatch(r"(\d+)(?:-(\d+))?", page_range)
            if not match:
                self.add_error(
                    "page_range",
                    "Enter a page number or range, such as 3-8.",
                )
                return cleaned_data
            first_page = int(match.group(1))
            last_page = int(match.group(2) or first_page)
            if first_page < 1 or last_page < first_page:
                self.add_error(
                    "page_range",
                    "Page ranges must start at 1 and end after they start.",
                )
                return cleaned_data
            if last_page > settings.PDF_MAX_PAGES:
                self.add_error(
                    "page_range",
                    f"Page ranges cannot exceed {settings.PDF_MAX_PAGES}.",
                )
                return cleaned_data

        if source_file:
            filename = source_file.name.lower()
            is_pdf = filename.endswith(".pdf")
            max_size = (
                settings.PDF_MAX_FILE_BYTES
                if is_pdf
                else settings.NOTES_MAX_FILE_BYTES
            )
            if source_file.size > max_size:
                limit_mb = max_size // (1024 * 1024)
                file_type = "PDF" if is_pdf else "Text files"
                raise forms.ValidationError(
                    f"{file_type} must be {limit_mb} MB or smaller."
                )

            if not is_pdf and not filename.endswith(".txt"):
                raise forms.ValidationError("Upload a plain-text .txt or .pdf file.")
            if page_range and not is_pdf:
                self.add_error("page_range", "Page ranges can only be used with PDFs.")
                return cleaned_data
            cleaned_data["notes"] = ""
        elif not notes:
            raise forms.ValidationError("Enter some notes or choose a text file.")
        elif page_range:
            self.add_error("page_range", "Page ranges can only be used with PDFs.")
        else:
            cleaned_data["notes"] = notes

        return cleaned_data
