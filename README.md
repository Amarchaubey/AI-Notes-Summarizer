# AI Notes Summarizer

A Django application that turns pasted notes, UTF-8 `.txt` uploads, or PDFs into source-grounded study summaries. Signed-in users can save their summaries and download them as PDFs.

PDF uploads support files up to 10 MB and 500 pages, with extracted text limited to 200,000 characters. Optionally enter a page or range such as `3-8` to skip irrelevant pages. Scanned/image-only PDFs use PyMuPDF OCR if Tesseract OCR and its English language data are installed locally; otherwise the app explains how to enable OCR.

Summarization runs as a persistent background job, so OCR and long documents do not hold the upload request open. For uploads, the app presents extracted text for review and editing before summarizing; uploaded files are removed after extraction. Jobs resume after a local server restart. Exact duplicate source text is cached per account and summary settings. The single-process local worker intentionally runs one CPU summarization at a time to avoid loading competing model copies. For multi-worker/production hosting, replace this in-process executor with a dedicated task queue such as Celery and Redis.

Chapter parsing cleans page numbers, repeated headers, table-of-contents pages, and groups headings with their content before selecting source statements. Choose short, balanced, or detailed output for each summary. Each saved summary also includes recall flashcards and a multiple-choice quiz generated from its key points. Quiz attempts, individual topic results, scores, strengths, and topics to revisit are saved. Rate each flashcard from Again to Easy to schedule its next review using a spaced-repetition interval. Select the optional flowchart checkbox to visualize summary points as ordered steps and inferred decision points. These study aids are derived from the summary and should be checked against the source notes.

## Run locally

Use Python 3.10 or newer. From this project directory:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Open <http://127.0.0.1:8000/> and register an account. By default, summaries select important sentences directly from the source notes, keeping chapter facts and definitions grounded instead of paraphrasing them into unrelated material. Detected tables of contents and repeated PDF headers are excluded. Choose a more detailed summary to include more source statements.

Scanned PDF OCR uses Tesseract OCR and its English language data. On Windows, install Tesseract OCR; the app checks its standard installation directory. If the language-data directory is elsewhere, set `PDF_TESSDATA_PATH` to the directory containing `eng.traineddata`. OCR is optional; text-based PDFs work without it.

An experimental abstractive mode using Hugging Face can be enabled with `AI_SUMMARIZATION_MODE=abstractive`; it may paraphrase or introduce errors, so the extractive default is recommended for exam notes. `HF_SUMMARIZATION_MODEL` selects the compatible model in that mode. `DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, and `DJANGO_ALLOWED_HOSTS` can also be set as environment variables when deploying. The large PyTorch/Transformers dependencies are optional: install `requirements-ai.txt` to enable this mode.

## Deploy with Docker

The included Dockerfile installs Tesseract, configures Gunicorn/WhiteNoise, collects static files, and applies migrations at container startup. For example:

```powershell
docker build -t ai-notes-summarizer .
docker run --rm -p 10000:10000 `
  -e DJANGO_DEBUG=0 `
  -e DJANGO_SECRET_KEY="your-generated-secret" `
  -e DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1 `
  -e DATABASE_URL="postgresql://user:password@host:5432/database" `
  ai-notes-summarizer
```

Use a managed PostgreSQL database for public hosting; SQLite inside a container is ephemeral. Configure the public host in `DJANGO_ALLOWED_HOSTS`, the HTTPS origin in `DJANGO_CSRF_TRUSTED_ORIGINS` (unless your host exposes `RENDER_EXTERNAL_HOSTNAME`), and set `DJANGO_SECRET_KEY` to a unique generated secret. After confirming your domain is always served over HTTPS, you may set `DJANGO_SECURE_HSTS_SECONDS` (for example, `31536000`) to enable HSTS. Never commit production credentials. The `/healthz/` endpoint checks database connectivity.

This local folder is not connected to a Git remote. Create a repository in your Git hosting account and add its remote URL before pushing.

## Tests

```powershell
python manage.py test
```
