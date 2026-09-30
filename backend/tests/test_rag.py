"""RAG for intent=question: ingest, retrieve, answer, eval. No network: embeddings are
the offline hashing function and Sarvam is faked."""

import ast
import shutil
from pathlib import Path

import pymupdf
import pytest
import yaml

from app import config
from app.clients import sarvam
from app.rag import answer as rag_answer
from app.rag import eval as rag_eval
from app.rag import index, ingest, retrieve
from app.services import i18n

EXAMPLE_INSURER = "Example General Insurance"
BACKEND = Path(__file__).resolve().parent.parent


# --- Chunking ----------------------------------------------------------------


def test_chunks_split_on_clause_headings():
    page = (
        "1. Room rent\nRoom rent is limited to 1% of the sum insured per day.\n"
        "2. Waiting periods\nPre-existing diseases are covered after 36 months of continuous cover.\n"
        "3. Exclusions\nCosmetic surgery is not covered. Dental treatment is not covered unless caused by an accident."
    )
    chunks = ingest.chunk_page(page)
    assert [c.split("\n")[0] for c in chunks] == ["1. Room rent", "2. Waiting periods", "3. Exclusions"]


def test_markdown_and_section_headings_count_as_clauses():
    page = "# Schedule\nSum insured Rs 5,00,000 for the policy year.\n\nSection 4 Claims\nIntimate the insurer within 24 hours of an emergency admission."
    assert len(ingest.chunk_page(page, min_chars=10)) == 2


def test_a_tiny_heading_is_merged_into_the_clause_after_it():
    page = "PART A\n1. Room rent\nRoom rent is limited to 1% of the sum insured per day, for the whole stay."
    chunks = ingest.chunk_page(page)
    assert len(chunks) == 1 and chunks[0].startswith("PART A")


def test_text_without_headings_falls_back_to_overlapping_windows():
    text = " ".join(f"word{i}" for i in range(1200))  # about 8,000 characters, no headings
    chunks = ingest.chunk_page(text, max_chars=2500, overlap=300)
    assert len(chunks) >= 3
    assert all(len(c) <= 2500 for c in chunks)
    assert chunks[0][-300:] == chunks[1][:300]


def test_an_over_long_clause_is_windowed_too():
    page = "1. Definitions\n" + "x" * 6000
    assert all(len(c) <= 2500 for c in ingest.chunk_page(page))


def test_pdf_pages_are_read_with_their_page_numbers(tmp_path):
    doc = pymupdf.open()
    for text in ("Page one text", "Page two text"):
        doc.new_page().insert_text((72, 72), text)
    path = tmp_path / "doc.pdf"
    doc.save(path)
    assert [(n, t.strip()) for n, t in ingest.read_pages(path)] == [(1, "Page one text"), (2, "Page two text")]


def test_markdown_page_markers_give_page_numbers(tmp_path):
    path = tmp_path / "doc.md"
    path.write_text("Intro\n<!-- page 2 -->\nSecond\n<!-- page 3 -->\nThird", encoding="utf-8")
    assert [(n, t.strip()) for n, t in ingest.read_pages(path)] == [(1, "Intro"), (2, "Second"), (3, "Third")]


# --- sources.yaml ------------------------------------------------------------

POLICY_TEXT = (
    "1. Room rent\nRoom rent is limited to 1% of the sum insured per day.\n"
    "<!-- page 2 -->\n"
    "2. Waiting periods\nPre-existing diseases are covered after 36 months of continuous cover.\n"
    "3. Claims\nIntimate the insurer within 24 hours of an emergency admission."
)
OTHER_TEXT = "1. Room rent\nRoom rent is limited to 2% of the sum insured per day under the other insurer's plan."
MOTOR_TEXT = "1. Depreciation\nZero depreciation cover removes depreciation on replaced parts."
REG_TEXT = "1. Grievance\nAn insurer shall resolve a grievance within the period the regulation sets."


def make_corpus(root: Path, entries=None) -> Path:
    corpus = root / "corpus"
    files = {
        "insurer/example_general/health_policy/wording.md": POLICY_TEXT,
        "insurer/other_insurer/health_policy/wording.md": OTHER_TEXT,
        "insurer/example_general/motor_policy/wording.md": MOTOR_TEXT,
        "regulation/grievance_rules.md": REG_TEXT,
    }
    for rel, text in files.items():
        (corpus / rel).parent.mkdir(parents=True, exist_ok=True)
        (corpus / rel).write_text(text, encoding="utf-8")
    entries = entries if entries is not None else [
        {"file": "insurer/example_general/health_policy/wording.md", "layer": "insurer", "insurer": EXAMPLE_INSURER,
         "product": "health_policy", "doc_type": "policy_wording", "source_url": "", "effective_date": "2026-04-01",
         "verified_by": "UNVERIFIED"},
        {"file": "insurer/other_insurer/health_policy/wording.md", "layer": "insurer", "insurer": "Other Insurer",
         "product": "health_policy", "doc_type": "policy_wording", "verified_by": "UNVERIFIED"},
        {"file": "insurer/example_general/motor_policy/wording.md", "layer": "insurer", "insurer": EXAMPLE_INSURER,
         "product": "motor_policy", "doc_type": "policy_wording", "verified_by": "UNVERIFIED"},
        {"file": "regulation/grievance_rules.md", "layer": "regulation", "doc_type": "regulation",
         "source_url": "https://example.invalid/reg", "verified_by": "A. Reviewer, 2026-10-01"},
    ]
    (corpus / "sources.yaml").write_text(yaml.safe_dump({"sources": entries}), encoding="utf-8")
    return corpus


@pytest.fixture
def corpus(tmp_path):
    return make_corpus(tmp_path)


@pytest.fixture
def collection(tmp_path, corpus):
    col = index.open_collection(tmp_path / "index", index.HashingEmbedding())
    ingest.ingest(corpus, col)
    return col


@pytest.mark.parametrize(
    "bad",
    [
        {"layer": "blog"},
        {"product": None},
        {"verified_by": None},
        {"file": "insurer/example_general/health_policy/missing.md"},
        {"product": "motor_policy"},  # file sits in the health_policy folder
        {"layer": "regulation"},  # file sits under insurer/
    ],
)
def test_a_bad_source_entry_is_refused(tmp_path, bad):
    entry = {"file": "insurer/example_general/health_policy/wording.md", "layer": "insurer", "insurer": EXAMPLE_INSURER,
             "product": "health_policy", "doc_type": "policy_wording", "verified_by": "UNVERIFIED"}
    entry.update(bad)
    entry = {k: v for k, v in entry.items() if v is not None}
    corpus = make_corpus(tmp_path, [entry])
    with pytest.raises(ingest.SourceError):
        ingest.load_sources(corpus)


# --- Ingest: stable ids, hashing, changed files only ---------------------------


def test_first_ingest_adds_every_file(tmp_path, corpus):
    col = index.open_collection(tmp_path / "index", index.HashingEmbedding())
    report = ingest.ingest(corpus, col)
    assert len(report.added) == 4 and report.updated == report.unchanged == report.removed == []
    ids = col.get()["ids"]
    assert "insurer/example_general/health_policy/wording.md#p2#c0" in ids


def test_rerun_skips_unchanged_files(tmp_path, collection, corpus):
    report = ingest.ingest(corpus, collection)
    assert len(report.unchanged) == 4 and not report.added and not report.updated


def test_a_changed_file_is_reingested_and_its_old_chunks_deleted(collection, corpus):
    path = corpus / "insurer/example_general/health_policy/wording.md"
    path.write_text("1. Room rent\nRoom rent is limited to 1.5% of the sum insured per day.", encoding="utf-8")
    report = ingest.ingest(corpus, collection)

    assert report.updated == ["insurer/example_general/health_policy/wording.md"]
    ids = [i for i in collection.get()["ids"] if i.startswith("insurer/example_general/health_policy/")]
    assert ids == ["insurer/example_general/health_policy/wording.md#p1#c0"]
    [doc] = collection.get(ids=ids)["documents"]
    assert "1.5%" in doc


def test_a_metadata_change_alone_reingests_the_file(collection, corpus):
    sources = yaml.safe_load((corpus / "sources.yaml").read_text(encoding="utf-8"))
    sources["sources"][0]["verified_by"] = "A. Reviewer, 2026-10-02"
    (corpus / "sources.yaml").write_text(yaml.safe_dump(sources), encoding="utf-8")

    assert ingest.ingest(corpus, collection).updated == ["insurer/example_general/health_policy/wording.md"]
    metas = collection.get(where={"file": "insurer/example_general/health_policy/wording.md"})["metadatas"]
    assert {m["verified_by"] for m in metas} == {"A. Reviewer, 2026-10-02"}


def test_a_file_dropped_from_sources_is_removed_from_the_index(collection, corpus):
    sources = yaml.safe_load((corpus / "sources.yaml").read_text(encoding="utf-8"))
    sources["sources"] = [s for s in sources["sources"] if s["layer"] != "regulation"]
    (corpus / "sources.yaml").write_text(yaml.safe_dump(sources), encoding="utf-8")

    assert ingest.ingest(corpus, collection).removed == ["regulation/grievance_rules.md"]
    assert not collection.get(where={"layer": "regulation"})["ids"]


def test_chunks_carry_their_source_metadata(collection):
    [meta] = collection.get(ids=["insurer/example_general/health_policy/wording.md#p1#c0"])["metadatas"]
    assert meta["insurer"] == EXAMPLE_INSURER
    assert meta["product"] == "health_policy"
    assert meta["doc_type"] == "policy_wording"
    assert meta["page"] == 1
    assert meta["verified_by"] == "UNVERIFIED"
    assert meta["effective_date"] == "2026-04-01"


# --- Retrieve ------------------------------------------------------------------


def test_retrieval_is_limited_to_her_insurer_and_product_or_regulation(collection):
    passages = retrieve.retrieve("room rent limit per day", insurer=EXAMPLE_INSURER, product="health_policy",
                                 collection=collection)
    owners = {(p.layer, p.insurer, p.product) for p in passages}
    assert owners <= {("insurer", EXAMPLE_INSURER, "health_policy"), ("regulation", "", "")}
    assert any(p.page == 1 and "1%" in p.text for p in passages)


def test_without_her_insurer_only_regulation_is_searched(collection):
    passages = retrieve.retrieve("room rent limit", collection=collection)
    assert passages and {p.layer for p in passages} == {"regulation"}


def test_at_most_six_passages(collection):
    assert len(retrieve.retrieve("room", insurer=EXAMPLE_INSURER, product="health_policy", collection=collection)) <= 6


def test_an_empty_index_returns_nothing(tmp_path):
    col = index.open_collection(tmp_path / "empty", index.HashingEmbedding())
    assert retrieve.retrieve("anything", insurer=EXAMPLE_INSURER, product="health_policy", collection=col) == []


# --- Answer ----------------------------------------------------------------------


@pytest.fixture
def model(monkeypatch):
    """A fake Sarvam: the test sets .reply; prompts are recorded."""
    state = type("Model", (), {"reply": None, "prompts": [], "systems": [], "translations": []})()

    def chat_json(messages, **kwargs):
        state.systems.append(messages[0]["content"])
        state.prompts.append(messages[-1]["content"])
        return state.reply

    def translate(text, target, source_language="auto", **kwargs):
        state.translations.append((source_language, target, text))
        return f"<{target}>{text}</{target}>"

    monkeypatch.setattr(sarvam, "chat_json", chat_json)
    monkeypatch.setattr(sarvam, "translate", translate)
    i18n._CACHE.clear()
    yield state
    i18n._CACHE.clear()


def ask(collection, question="What is the room rent limit?", **kwargs):
    kwargs.setdefault("insurer", EXAMPLE_INSURER)
    kwargs.setdefault("product", "health_policy")
    return rag_answer.answer(question, intent="question", collection=collection, **kwargs)


def source_number(model, page):
    """The [S#] the prompt gave the example wording's page."""
    prompt = model.prompts[-1]
    for line in prompt.splitlines():
        if line.startswith("[S") and f"p.{page}" in line and EXAMPLE_INSURER in line:
            return line.split("]")[0] + "]"
    raise AssertionError(prompt)


def test_an_answer_cites_insurer_doc_type_and_page(collection, model):
    model.reply = None
    ask(collection)  # first call to learn the source numbering
    s = source_number(model, 1)
    model.reply = {"answer": f"Room rent is limited to 1% of the sum insured per day {s}.", "sources": [s.strip("[]")]}
    result = ask(collection)

    assert result.status == rag_answer.ANSWERED
    assert result.text == f"Room rent is limited to 1% of the sum insured per day [{EXAMPLE_INSURER}, policy_wording, p.1]."
    assert [(c.insurer, c.doc_type, c.page) for c in result.citations] == [(EXAMPLE_INSURER, "policy_wording", 1)]
    assert result.unverified is True  # the cited wording is UNVERIFIED


def test_the_prompt_holds_only_the_retrieved_sources_and_the_rules(collection, model):
    ask(collection)
    prompt = model.prompts[-1]
    assert model.systems[-1] == rag_answer.SYSTEM_PROMPT
    assert "NO_SOURCE" in rag_answer.SYSTEM_PROMPT and "never" in rag_answer.SYSTEM_PROMPT.lower()
    assert "Other Insurer" not in prompt
    assert "2% of the sum insured" not in prompt


def test_no_source_hands_off_to_the_insurer_through_the_router(collection, model):
    model.reply = {"answer": "NO_SOURCE", "sources": []}
    result = ask(collection, "What is the claim settlement ratio?", names={"insurer": "Example General Insurance Company Ltd"})

    assert result.status == rag_answer.NO_SOURCE
    assert result.citations == ()
    assert result.handoff == {
        "respondent": "insurer",
        "respondent_name": "Example General Insurance Company Ltd",
        "step": "coverage_query",
        "ladder": "coverage_question",
        "distributor_owned": False,
    }
    assert "will not guess" in result.text_en


def test_no_source_without_a_known_product_asks_which_policy(collection, model):
    model.reply = {"answer": "NO_SOURCE", "sources": []}
    result = rag_answer.answer("anything", intent="question", collection=collection)
    assert result.status == rag_answer.NO_SOURCE
    assert result.handoff is None


def test_nothing_retrieved_is_no_source_without_asking_the_model(tmp_path, model):
    empty = index.open_collection(tmp_path / "empty", index.HashingEmbedding())
    result = ask(empty)
    assert result.status == rag_answer.NO_SOURCE
    assert model.prompts == []


@pytest.mark.parametrize(
    "reply",
    [
        {"answer": "Room rent is limited to 1% of the sum insured.", "sources": []},  # no citation
        {"answer": "Room rent is limited [S9].", "sources": ["S9"]},  # cites a source it was not given
        None,  # unreadable reply
    ],
)
def test_an_unsupported_answer_becomes_no_source(collection, model, reply):
    model.reply = reply
    assert ask(collection).status == rag_answer.NO_SOURCE


def test_it_never_promises_approval(collection, model):
    ask(collection)
    s = source_number(model, 1)
    model.reply = {
        "answer": f"Room rent is limited to 1% of the sum insured per day {s}. Your claim will be approved. "
                  f"Don't worry, you will definitely get paid.",
        "sources": [s.strip("[]")],
    }
    result = ask(collection)
    assert result.status == rag_answer.ANSWERED
    lowered = result.text_en.lower()
    assert "approved" not in lowered and "definitely" not in lowered and "get paid" not in lowered


def test_only_questions_are_answered(collection):
    with pytest.raises(rag_answer.NotAQuestion):
        rag_answer.answer("my claim was rejected", intent="grievance", collection=collection)


def test_marathi_in_and_out_keeping_amounts_and_citations_untranslated(collection, model):
    question = "माझ्या पॉलिसीत रूम भाड्याची मर्यादा किती आहे?"
    ask(collection, question, language="mr-IN")  # learn how this question's sources are numbered
    s = source_number(model, 1)
    model.reply = {"answer": f"Room rent is limited to 1% of ₹5,00,000 per day {s}.", "sources": [s.strip("[]")]}
    result = ask(collection, question, language="mr-IN")

    source, target, _ = model.translations[0]
    assert (source, target) == ("mr-IN", "en-IN")  # the question went in as English
    out = model.translations[-1][2]
    assert "1%" not in out and "₹5,00,000" not in out and EXAMPLE_INSURER not in out  # protected from translation
    assert result.translated is True
    assert result.text.startswith("<mr-IN>")
    assert "1%" in result.text and "₹5,00,000" in result.text
    assert f"[{EXAMPLE_INSURER}, policy_wording, p.1]" in result.text


def test_if_translation_mangles_a_protected_value_the_english_answer_is_sent(collection, model, monkeypatch):
    ask(collection)
    s = source_number(model, 1)
    model.reply = {"answer": f"Room rent is limited to 1% of the sum insured per day {s}.", "sources": [s.strip("[]")]}
    monkeypatch.setattr(sarvam, "translate", lambda text, target, **k: "सर्व काही बदलले")
    result = ask(collection, "रूम भाडे?", language="mr-IN")
    assert result.translated is False
    assert result.text == result.text_en


# --- Hard rule: RAG never writes to Facts; the engine never sees RAG ------------

RAG_DIR = BACKEND / "app" / "rag"
FORBIDDEN = {"app.core.ladder_engine", "app.store", "app.services.documents", "app.cases"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found |= {f"{node.module}.{alias.name}" for alias in node.names}
    return found


@pytest.mark.parametrize("path", sorted(RAG_DIR.glob("*.py")), ids=lambda p: p.name)
def test_rag_cannot_reach_facts_or_the_store(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    assert not _imports(path) & FORBIDDEN
    assert not names & {"Facts", "evaluate", "record_event", "save_document", "set_route"}


def test_the_engine_and_router_never_import_rag():
    for name in ("ladder_engine.py", "routing.py", "ladders.py"):
        assert not any(i.startswith("app.rag") for i in _imports(BACKEND / "app" / "core" / name))


# --- The repo corpus, sources.yaml and golden set ------------------------------


def test_repo_sources_yaml_is_valid():
    sources = ingest.load_sources(config.CORPUS_DIR)
    assert sources and all(s.verified_by for s in sources)


def test_repo_corpus_answers_the_golden_questions_offline(tmp_path):
    col = index.open_collection(tmp_path / "index", index.HashingEmbedding())
    ingest.ingest(config.CORPUS_DIR, col)
    for example in rag_eval.load_golden():
        if example["expect"] != "answer" or example.get("language", "en-IN") != "en-IN":
            continue
        passages = retrieve.retrieve(example["question"], insurer=example["insurer"], product=example["product"],
                                     collection=col)
        pages = {p.page for p in passages if p.doc_type == example["cite"]["doc_type"]}
        assert example["cite"]["page"] in pages, example["id"]


def test_golden_set_has_five_examples_with_a_refusal():
    examples = rag_eval.load_golden()
    assert len(examples) == 5
    assert sum(e["expect"] == "no_source" for e in examples) >= 1


# --- Eval metrics ----------------------------------------------------------------


def test_eval_scores_accuracy_citations_and_refusals():
    Citation = rag_answer.Citation

    def fake(example):
        if example["id"] == "refuse":
            return rag_answer.Answer(rag_answer.NO_SOURCE, "x", "x", (), False, None, "en-IN", False)
        if example["id"] == "good":
            cite = (Citation("Ins", "policy_wording", 2, "", "UNVERIFIED"),)
            return rag_answer.Answer(rag_answer.ANSWERED, "36 months", "36 months", cite, True, None, "en-IN", False)
        return rag_answer.Answer(rag_answer.ANSWERED, "wrong", "wrong", (), False, None, "en-IN", False)

    examples = [
        {"id": "good", "expect": "answer", "must_contain": ["36 months"], "cite": {"doc_type": "policy_wording", "page": 2}},
        {"id": "bad", "expect": "answer", "must_contain": ["24 months"]},
        {"id": "refuse", "expect": "no_source"},
    ]
    report = rag_eval.run(examples, fake)
    assert report["accuracy"] == pytest.approx(2 / 3)
    assert report["citation_rate"] == pytest.approx(1 / 2)
    assert report["correct_refusal_rate"] == 1.0
