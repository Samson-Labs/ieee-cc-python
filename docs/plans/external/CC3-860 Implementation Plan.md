# CC3-860: Extend Bulk Pipeline for Backfill

## Context

Legacy catalog items (pre-2023) lack descriptions/tags. CC3-860 extends the bulk pipeline to support backfilling these items via three new capabilities: text-only items (no file), text+file hybrid, and cross-bucket file copy. Depends on CC3-858 (PR #30, approved) which added direct invocation + input_text support to the orchestrator. Branch from `feature/cc3-858-input-text-selective-fields`.

## Files to Modify

| File | Changes |
|------|---------|
| `src/bulk/bulk_processor.py` | Relax validation (s3_key optional when input_text present), validate new fields, add "text" cost estimate |
| `src/bulk/bulk_worker.py` | Three-path routing (text-only/hybrid/file-only), cross-bucket copy, `_invoke_orchestrator_direct()` |
| `tests/bulk/test_bulk_processor.py` | Validation: text-only accepted, neither rejected, invalid mode/fields rejected, cost estimation |
| `tests/bulk/test_bulk_worker.py` | Text-only path, hybrid path, cross-bucket copy, backward compat |

## Step 1: bulk_processor.py — Validation + Cost

**1a. Split required fields:**
```python
ALWAYS_REQUIRED_ITEM_FIELDS = {"item_id", "request_id", "resource_center"}
FILE_REQUIRED_ITEM_FIELDS = {"s3_key", "media_type"}
```

**1b. Add validation constants:**
```python
VALID_INPUT_TEXT_MODES = frozenset({"as_source", "as_abstract"})
```
Keep `VALID_MEDIA_TYPES` limited to the original 4 file-backed types; do **not** add `"text"`.
Text-only items are represented by omitting `media_type` and providing `input_text`.

**1c. Rewrite `_validate_manifest()` per-item logic:**
- Check `ALWAYS_REQUIRED_ITEM_FIELDS` present
- Require at least one of `s3_key` or `input_text`
- If `s3_key` present: require `media_type` in original 4 types
- If `input_text_mode` present: validate in `VALID_INPUT_TEXT_MODES`
- If `requested_fields` present: validate non-empty list (skip field-name validation to avoid cross-Lambda import of `ALL_FIELDS`)

**1d. Update `_estimate_cost()`:**
- Add `"text": 0.005` to `COST_ESTIMATES`
- Use `item.get("media_type", "text")` to handle text-only items without media_type

## Step 2: bulk_worker.py — Three-Path Routing

**2a. Cross-bucket in `_copy_to_pending()`** (one-line change):
```python
source_bucket = item.get("source_bucket", bucket)
# CopySource uses source_bucket instead of always bucket
```

**2b. Hybrid path in `_create_meta_json()`:**
After building meta dict, conditionally add:
```python
if item.get("input_text"):
    meta["input_text"] = item["input_text"]
    meta["input_text_mode"] = item.get("input_text_mode", "as_source")
if item.get("requested_fields"):
    meta["requested_fields"] = item["requested_fields"]
```

**2c. Text-only path — new `_invoke_orchestrator_direct()`:**
Builds CC3-858 direct invocation payload:
```python
{"bucket": bucket, "meta": {
    "item_id", "ou", "product_part_number", "ai_enrichment_enabled": True,
    "callback_url", "input_text", "input_text_mode",
    "content": {"media_type": "text/plain"},
    # + requested_fields if present
}}
```
Uses same Lambda invoke + error handling as `_invoke_orchestrator()`.

**2d. Route in `process_item()`:**
```python
has_file = bool(item.get("s3_key"))
has_text = bool(item.get("input_text"))

if has_text and not has_file:
    # Text-only: skip copy + meta, direct invocation
    self._invoke_orchestrator_direct(bucket, item, callback_url)
else:
    # File path (with or without text): copy + meta + standard invocation
    pending_key = self._copy_to_pending(bucket, item)
    self._create_meta_json(bucket, item, callback_url)
    self._invoke_orchestrator(bucket, pending_key)
```
Progress tracking + completion notification unchanged for all paths.

## Step 3: Tests

### test_bulk_processor.py (new tests)
| Test | What |
|------|------|
| `test_text_only_item_accepted` | input_text, no s3_key, no media_type passes |
| `test_hybrid_item_accepted` | input_text + s3_key + media_type passes |
| `test_neither_text_nor_key_rejected` | Missing both raises ValidationError |
| `test_file_without_media_type_rejected` | s3_key without media_type raises |
| `test_invalid_input_text_mode_rejected` | Bad mode raises |
| `test_empty_requested_fields_rejected` | Empty array raises |
| `test_text_only_cost_estimate` | Text items at $0.005 |
| `test_backward_compat_existing_manifest` | Standard manifest unchanged |

### test_bulk_worker.py (new tests)
| Test | What |
|------|------|
| `test_text_only_skips_copy_and_meta` | No copy_object, no meta put_object |
| `test_text_only_direct_invocation_format` | Payload has `{"bucket", "meta"}` |
| `test_text_only_meta_fields_correct` | meta includes input_text, ou, item_id, content |
| `test_text_only_with_requested_fields` | requested_fields forwarded |
| `test_text_only_with_as_abstract` | input_text_mode forwarded |
| `test_hybrid_copies_file_and_adds_text` | copy_object called, meta has input_text |
| `test_hybrid_uses_standard_key_path` | Payload has `{"bucket", "key"}` |
| `test_cross_bucket_uses_source_bucket` | CopySource.Bucket = source_bucket |
| `test_no_source_bucket_uses_default` | CopySource.Bucket = pipeline bucket |
| `test_backward_compat_file_only` | Existing flow unchanged |

## Execution Order

1. `git checkout -b feature/cc3-860-bulk-backfill-extension feature/cc3-858-input-text-selective-fields`
2. Implement bulk_processor.py changes + tests
3. Implement bulk_worker.py changes + tests
4. Run full suite: `python -m pytest tests/ -v`

## Verification

```bash
python -m pytest tests/bulk/ -v          # All bulk tests pass
python -m pytest tests/ -v               # Full suite (444+ tests pass)
```

## Branching Strategy

CC3-860 depends on CC3-858. Two options:

### Option A: Stack on CC3-858 branch (recommended)
```
development
  └── feature/cc3-858-input-text-selective-fields (PR #30)
        └── feature/cc3-860-bulk-backfill-extension (new PR)
```
- Branch from `feature/cc3-858-input-text-selective-fields`
- PR #31 targets `feature/cc3-858-input-text-selective-fields` (not development)
- After PR #30 merges to development, retarget PR #31 to development
- **Pro**: Can start immediately, no waiting for merge
- **Con**: Stacked PRs add merge-order dependency

### Option B: Wait for CC3-858 merge
- Merge PR #30 into development first
- Branch from development
- PR targets development directly
- **Pro**: Clean, single PR, no retargeting
- **Con**: Blocked until PR #30 merges

**Recommendation**: Option A — PR #30 is approved and ready; stack the branch so we can work now. Retarget after merge.

## Notes

- **No cross-Lambda imports**: Don't import `ALL_FIELDS` from bedrock_inference into bulk_processor — they're separate Docker containers. Validate requested_fields shape only (list, non-empty).
- **IAM**: Cross-bucket copy needs `s3:GetObject` on source bucket — tracked by CC3-861 (Anadi).
- **`content.media_type`** for text-only: use `"text/plain"`. The orchestrator skips extraction when `input_text` is present (CC3-858 logic), so this value is never routed on.
