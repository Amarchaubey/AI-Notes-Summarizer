"""Source-grounded note summaries and study aids."""

import html
import math
import os
import re
import random
import shutil
import unicodedata
from collections import Counter
from functools import lru_cache
from pathlib import Path


DEFAULT_MODEL_NAME = "sshleifer/distilbart-cnn-12-6"
CHUNK_TOKEN_LIMIT = 900
COMMON_WORDS = {
    "about", "after", "again", "also", "because", "before", "being", "between",
    "could", "every", "first", "from", "have", "into", "main", "more", "most",
    "notes", "often", "other", "over", "should", "some", "such", "than", "that",
    "their", "them", "then", "there", "these", "they", "this", "through", "under",
    "very", "what", "when", "where", "which", "while", "with", "would", "your",
}
NON_CONCEPT_SUBJECTS = {"it", "this", "that", "these", "those", "they", "we"}


def _tessdata_directory():
    from django.conf import settings

    candidates = [
        getattr(settings, "PDF_TESSDATA_PATH", None),
        os.environ.get("TESSDATA_PREFIX"),
    ]
    executable = shutil.which("tesseract")
    if executable:
        candidates.append(str(Path(executable).resolve().parent / "tessdata"))
    candidates.extend(
        [
            r"C:\Program Files\Tesseract-OCR\tessdata",
            "/usr/share/tesseract-ocr/5/tessdata",
            "/usr/share/tesseract-ocr/4.00/tessdata",
            "/usr/share/tessdata",
        ]
    )
    for candidate in candidates:
        if candidate and (Path(candidate) / "eng.traineddata").is_file():
            return str(candidate)
    return None


def extract_pdf_text(pdf_bytes, page_range=""):
    """Extract searchable text from a PDF, rejecting oversized or image-only files."""
    import pymupdf
    from django.conf import settings

    try:
        document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except (pymupdf.FileDataError, pymupdf.EmptyFileError, RuntimeError) as exc:
        raise ValueError("This file is not a valid, readable PDF.") from exc

    with document:
        if document.is_encrypted:
            raise ValueError("Password-protected PDFs are not supported.")
        if document.page_count > settings.PDF_MAX_PAGES:
            raise ValueError(
                f"PDFs must have {settings.PDF_MAX_PAGES} pages or fewer."
            )

        start_page, end_page = 0, document.page_count
        if page_range:
            bounds = [int(part) for part in page_range.split("-")]
            start_page = bounds[0] - 1
            end_page = bounds[-1]
            if start_page >= document.page_count or end_page > document.page_count:
                raise ValueError(
                    f"This PDF has {document.page_count} pages; update the page range."
                )

        pages = []
        character_count = 0
        for page in document.pages(start_page, end_page):
            page_text = page.get_text().strip()
            if not page_text and settings.PDF_OCR_ENABLED:
                try:
                    tessdata = _tessdata_directory()
                    if not tessdata:
                        raise RuntimeError("English Tesseract data was not found.")
                    text_page = page.get_textpage_ocr(
                        language="eng",
                        dpi=150,
                        tessdata=tessdata,
                    )
                    page_text = page.get_text(textpage=text_page).strip()
                except RuntimeError as exc:
                    raise ValueError(
                        "This PDF needs OCR, but Tesseract OCR is not available. "
                        "Install Tesseract and its English language data, then "
                        "restart the app."
                    ) from exc
            character_count += len(page_text)
            if character_count > settings.NOTES_MAX_CHARACTERS:
                raise ValueError(
                    "This PDF contains more than "
                    f"{settings.NOTES_MAX_CHARACTERS:,} characters of text. "
                    "Please split it into smaller documents."
                )
            if page_text:
                pages.append(page_text)

    extracted_text = "\n\n".join(pages).strip()
    if not extracted_text:
        raise ValueError(
            "No readable text was found in this PDF. It may be blank, damaged, "
            "or too low-resolution for OCR."
        )
    return extracted_text


@lru_cache(maxsize=1)
def _summarizer():
    from transformers import AutoModelForSeq2SeqLM, AutoTokenizer, pipeline

    model_name = os.environ.get("HF_SUMMARIZATION_MODEL", DEFAULT_MODEL_NAME)
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    model = AutoModelForSeq2SeqLM.from_pretrained(model_name)
    return pipeline("summarization", model=model, tokenizer=tokenizer, device=-1)


def _sentence_bullets(text):
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text.strip())
    return [f"- {sentence.strip()}" for sentence in sentences if sentence.strip()]


def create_study_aids(summary_text, include_flowchart=False):
    """Build review questions from source-grounded summary statements."""
    bullets = [
        line.lstrip("-* ").strip()
        for line in summary_text.splitlines()
        if line.strip()
    ]
    candidates = []
    seen_details = set()
    for bullet in bullets:
        topic_match = re.match(r"^\[([^\]]+)\]\s*(.*)$", bullet)
        topic = topic_match.group(1).strip() if topic_match else ""
        sentence = topic_match.group(2).strip() if topic_match else bullet
        definition = re.match(
            r"^(?:the\s+|a\s+|an\s+)?(.{2,80}?)\s+"
            r"(?:is|are|refers to|means|denotes|can be defined as)\s+(.+)$",
            sentence,
            re.I,
        )
        if definition:
            concept = re.sub(
                r"^(?:the|a|an)\s+",
                "",
                definition.group(1).strip(),
                flags=re.I,
            ).strip(" :-")
            if (
                concept.casefold() in NON_CONCEPT_SUBJECTS
                or len(concept.split()) > 5
                or re.search(
                    r"\b(when|while|because|before|after|unless|then|however|best)\b",
                    concept,
                    re.I,
                )
                or re.search(r"[;,:]", concept)
            ):
                definition = None
            else:
                prompt = f"According to your notes, what is {concept}?"
                answer = sentence
        if not definition:
            concept = topic or "this key idea"
            prompt = f"Recall a key point from {concept}."
            answer = sentence
        detail_key = sentence.casefold()
        if detail_key in seen_details:
            continue
        seen_details.add(detail_key)
        candidates.append(
            {
                "prompt": prompt,
                "answer": answer,
                "detail": sentence,
                "concept": concept,
                "is_definition": bool(definition),
            }
        )

    concepts = list(
        dict.fromkeys(
            card["concept"]
            for card in candidates
            if card["is_definition"]
        )
    )
    rng = random.Random(summary_text)
    for card in candidates:
        distractors = [
            concept
            for concept in concepts
            if concept.casefold() != card["concept"].casefold()
        ]
        rng.shuffle(distractors)
        card["options"] = distractors[:3] + [card["concept"]]

    flowchart = []
    if include_flowchart and len(bullets) >= 2:
        for bullet in bullets[:8]:
            if re.search(r"\b(if|when|whether|unless|otherwise)\b", bullet, re.I):
                flowchart.append({"type": "decision", "label": bullet})
            elif re.search(
                r"\b(then|next|first|finally|before|after|start|begin|step)\b",
                bullet,
                re.I,
            ):
                flowchart.append({"type": "step", "label": bullet})
            else:
                flowchart.append({"type": "point", "label": bullet})
    return {
        "flashcards": candidates[:10],
        "quiz": [
            {
                **card,
                "prompt": f"Which concept matches this description: {card['detail']}",
                "answer": card["concept"],
            }
            for card in candidates
            if card["is_definition"]
        ][:10] if len(concepts) >= 2 else [],
        "flowchart": flowchart,
    }


def _prepare_note_sections(text):
    text = unicodedata.normalize("NFKC", html.unescape(text))
    text = text.replace("\ufffd", " ").replace("\u00ad", "")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    table_headers = Counter(
        line.casefold()
        for line in lines
        if len(line) > 5 and not re.search(r"\d", line)
    )
    repeated_headers = {
        line
        for line, count in table_headers.items()
        if count >= 2 and len(line.split()) <= 12
    }

    cleaned_lines = []
    skip_contents = False
    for line in lines:
        if not line or re.fullmatch(r"page\s+\d+", line, re.I):
            if skip_contents and re.fullmatch(r"page\s+\d+", line, re.I):
                skip_contents = False
            continue
        if re.match(r"^table of contents\b", line, re.I):
            skip_contents = True
            continue
        if skip_contents:
            continue
        if re.match(r"^pps\s+\d+(?:st|nd|rd|th)\s+year\b", line, re.I):
            continue
        if re.match(r"^coverage\s*:", line, re.I):
            continue
        if line.casefold() in repeated_headers:
            continue
        if re.fullmatch(r"[\W_]+", line):
            continue
        if line.lstrip().startswith(("•", "�")):
            line = line.lstrip("•� \t")
            if line:
                cleaned_lines.append(line)
                continue
        cleaned_lines.append(line)

    first_chapter = next(
        (
            index
            for index, line in enumerate(cleaned_lines)
            if re.match(r"^\d+(?:\.\d+)*[.)]?\s+[A-Za-z]", line)
        ),
        None,
    )
    if first_chapter is not None:
        cleaned_lines = cleaned_lines[first_chapter:]

    sections = []
    current_topic = "Key points"
    current_parts = []
    heading_pattern = re.compile(
        r"^(?:\d+(?:\.\d+)*[.)]?\s+|[A-Z][A-Z0-9 &/()-]{3,}$)"
    )
    bullet_pattern = re.compile(r"^(?:[-*]\s+|\d+[.)]\s+)")

    def flush():
        if current_parts:
            sections.append((current_topic, " ".join(current_parts)))
            current_parts.clear()

    for line in cleaned_lines:
        heading = heading_pattern.match(line)
        if heading and len(line.split()) <= 14:
            flush()
            current_topic = re.sub(r"^\d+(?:\.\d+)*[.)]?\s+", "", line).strip()
            continue
        question_heading = re.match(r"^what\s+(?:is|are)\s+(.+?)\??$", line, re.I)
        if question_heading and len(line.split()) <= 12:
            flush()
            current_topic = question_heading.group(1).strip(" ?")
            continue
        if (
            len(line.split()) <= 8
            and not re.search(r"[.!?;:]$", line)
            and re.match(r"^[A-Z]", line)
            and not re.search(
                r"\b(is|are|was|were|means|refers|consists|includes|include|used|has|have|can|should)\b",
                line,
                re.I,
            )
            and len(line) <= 70
        ):
            flush()
            current_topic = line.strip(" :")
            continue
        if bullet_pattern.match(line):
            flush()
            line = bullet_pattern.sub("", line)
            if line:
                current_parts.append(line)
                if re.search(r"[.!?:;]$", line):
                    flush()
            continue
        if current_parts and current_parts[-1].endswith((",", ";", ":")):
            current_parts[-1] = f"{current_parts[-1]} {line}"
            if re.search(r"[.!?]$", line):
                flush()
            continue
        current_parts.append(line)
        if re.search(r"[.!?]$", line):
            flush()
    flush()

    candidates = []
    for topic, block in sections:
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", block):
            sentence = sentence.strip(" \t-*•")
            words = re.findall(r"\b[\w#]+(?:[.'+-][\w#]+)*\b", sentence)
            if len(words) < 7 or len(sentence) < 45:
                continue
            if re.match(r"^(?:contents|coverage|table of contents)\b", sentence, re.I):
                continue
            if re.search(r"^\s*(?:page\s+\d+|pps\s+\d+(?:st|nd|rd|th)\s+year)", sentence, re.I):
                continue
            candidates.append({"topic": topic, "text": sentence, "words": words})

    return candidates


def _select_source_statements(candidates, count):
    if not candidates:
        return []
    frequencies = Counter(
        word.casefold()
        for candidate in candidates
        for word in candidate["words"]
        if word.casefold() not in COMMON_WORDS and len(word) > 2
    )
    sentence_count = len(candidates)
    for candidate in candidates:
        words = [
            word.casefold()
            for word in candidate["words"]
            if word.casefold() not in COMMON_WORDS and len(word) > 2
        ]
        score = sum(
            math.log(1 + sentence_count / frequencies[word])
            for word in set(words)
        ) / math.sqrt(max(len(candidate["words"]), 1))
        candidate["is_definition"] = bool(
            re.match(
                r"^(?:a|an|the)?\s*[^;,:.]{2,55}\s+"
                r"(?:is|are|means|refers to|denotes|consists of|can be defined as)\b",
                candidate["text"],
                re.I,
            )
        )
        if re.match(
            r"\b(algorithm|flowchart|variable|array|pointer|function|loop|operator|structure|memory|file)\b",
            candidate["text"],
            re.I,
        ):
            score += 1.0
        if candidate["is_definition"]:
            score += 8.0
        if len(candidate["words"]) > 42:
            score *= 0.7
        candidate["score"] = score

    ranked = sorted(candidates, key=lambda candidate: candidate["score"], reverse=True)
    selected = []
    section_candidates = {}
    for candidate in ranked:
        section_candidates.setdefault(candidate["topic"], []).append(candidate)
    for topic, topic_candidates in section_candidates.items():
        best = next(
            (
                candidate
                for candidate in topic_candidates
                if candidate["is_definition"]
            ),
            topic_candidates[0],
        )
        selected.append(best)
        if len(selected) == count:
            break
    if len(selected) < count:
        selected_ids = {id(candidate) for candidate in selected}
        for candidate in ranked:
            if id(candidate) in selected_ids:
                continue
            terms = set(word.casefold() for word in candidate["words"])
            if any(
                len(terms & set(word.casefold() for word in chosen["words"]))
                / max(len(terms | set(word.casefold() for word in chosen["words"])), 1)
                > 0.8
                for chosen in selected
            ):
                continue
            selected.append(candidate)
            if len(selected) == count:
                break
    selected_ids = {id(candidate) for candidate in selected}
    return [
        candidate
        for candidate in candidates
        if id(candidate) in selected_ids
    ]


def _abstractive_summary(text, length):
    """Optional experimental generation; source-extractive mode is the default."""
    length_settings = {"short": (90, 12), "balanced": (180, 20), "detailed": (260, 35)}
    max_length, min_length = length_settings[length]
    summarizer = _summarizer()
    tokenizer = summarizer.tokenizer
    token_ids = tokenizer.encode(text, add_special_tokens=False)
    generated = []
    for index in range(0, len(token_ids), CHUNK_TOKEN_LIMIT):
        chunk = tokenizer.decode(
            token_ids[index : index + CHUNK_TOKEN_LIMIT],
            skip_special_tokens=True,
        )
        result = summarizer(
            chunk,
            max_length=max_length,
            min_length=min_length,
            do_sample=False,
            truncation=True,
        )
        generated.append(result[0]["summary_text"].strip())
    return "\n".join(_sentence_bullets(" ".join(generated)))


def summarize_notes(text, length="balanced"):
    """Select important statements directly from the notes to prevent invented facts."""
    limits = {"short": 10, "balanced": 24, "detailed": 40}
    if length not in limits:
        raise ValueError("Choose a short, balanced, or detailed summary.")
    if os.environ.get("AI_SUMMARIZATION_MODE", "extractive").lower() == "abstractive":
        return _abstractive_summary(text, length)
    candidates = _prepare_note_sections(text)
    selected = _select_source_statements(candidates, limits[length])
    if not selected:
        raise ValueError(
            "Could not find enough readable study content in these notes. "
            "Check the uploaded file's text and try again."
        )
    return "\n".join(
        f"- [{candidate['topic']}] {candidate['text']}"
        for candidate in selected
    )
