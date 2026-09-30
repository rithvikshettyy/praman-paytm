from app.services import blocks


def test_html_page_becomes_addressable_blocks_with_true_offsets():
    doc = blocks.normalize_pages(
        [{"page_num": 1, "content": "<h1>Schedule</h1><p>Sum insured 5 lakh.</p><li>Room cap 1%</li>"}]
    )
    page_text = doc.page_texts[1]

    assert [b.block_id for b in doc.blocks] == ["p1-b000", "p1-b001", "p1-b002"]
    assert [b.tag for b in doc.blocks] == ["heading", "paragraph", "list_item"]
    for block in doc.blocks:
        assert page_text[block.char_start : block.char_end] == block.text


def test_supplied_blocks_split_into_clauses_and_keep_bbox():
    long_line = "Clause one applies here. " * 12
    doc = blocks.normalize_pages(
        [
            {
                "page_num": 1,
                "blocks": [
                    {
                        "text": f"Short line\n{long_line}",
                        "layout_tag": "paragraph",
                        "coordinates": {"x1": 1, "y1": 2, "x2": 3, "y2": 4},
                    }
                ],
            }
        ]
    )

    assert len(doc.blocks) >= 3
    assert doc.blocks[0].text == "Short line"
    assert all(b.bbox == [1.0, 2.0, 3.0, 4.0] for b in doc.blocks)


def test_page_offset_makes_batch_pages_absolute():
    doc = blocks.normalize_pages([{"page_num": 1, "content": "<p>x</p>"}], page_offset=10)
    assert doc.blocks[0].page_number == 11
    assert doc.blocks[0].block_id == "p11-b000"


def test_merge_orders_batches_by_page():
    later = blocks.normalize_pages([{"page_num": 1, "content": "<p>b</p>"}], page_offset=10)
    earlier = blocks.normalize_pages([{"page_num": 1, "content": "<p>a</p>"}])
    merged = blocks.merge([later, earlier])

    assert [b.page_number for b in merged.blocks] == [1, 11]
    assert merged.page_count == 2


def test_find_span_exact_loose_and_fallback():
    block = blocks.normalize_pages(
        [{"page_num": 1, "content": "<p>Pre-existing   diseases wait 36 months.</p>"}]
    ).blocks[0]

    start, end = blocks.find_span(block, "wait 36 months")
    assert block.text[start - block.char_start : end - block.char_start] == "wait 36 months"

    start, end = blocks.find_span(block, "pre-existing diseases")
    assert (start, end) != (block.char_start, block.char_end)

    assert blocks.find_span(block, "not in the text") == (block.char_start, block.char_end)
