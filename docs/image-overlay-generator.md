# Image Overlay Generation Module

## Overview

Lambda function that generates product overlay images from JSON trigger files written by Drupal's ImageGenerationService. Reads the trigger JSON, loads a background image from S3, applies text overlays using Pillow, and writes output to the destination bucket.

**Module path:** `src/generators/image_overlay_generator.py`
**Handler path:** `src/handlers/image_overlay_handler.py`
**Lambda:** `ieee-rc-image-generator` (Python 3.12, Pillow, 1024 MB, 60s timeout)

## Usage

```python
from src.generators import ImageOverlayGenerator

generator = ImageOverlayGenerator(s3_client=boto3.client("s3"))
result = generator.process_trigger(
    bucket="trigger-bucket",
    key="actions/job-001.json",
)

print(result["output_key"])     # "images/products/STD-12345.jpg"
print(result["thumbnail_key"])  # "" or "images/products/STD-12345_thumb.jpg"
print(result["width"])          # 800
print(result["height"])         # 600
print(result["format"])         # "jpg"
```

For testing without S3:

```python
from PIL import Image

background = Image.open("background.jpg")
overlay = generator.generate_overlay(
    background=background,
    title="Product Title",
    authors="Author One, Author Two",
)
overlay.save("output.jpg")
```

## Trigger JSON Schema

Written by Drupal's ImageGenerationService to `actions/*.json`:

```json
{
  "product_part_number": "STD-12345",
  "title": "IEEE Standard for Something Important",
  "authors": "Jane Doe, John Smith, Alice Johnson",
  "config": {
    "source_bucket": "ieee-rc-assets",
    "dest_bucket": "ieee-rc-public",
    "public_path": "images/products"
  },
  "background_source": "ieee",
  "output_format": "jpg",
  "output_quality": 85,
  "is_thumbnail": false
}
```

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `product_part_number` | Yes | — | Used in output filename |
| `title` | Yes | — | Product title, word-wrapped to max 3 lines |
| `authors` | Yes | — | Author names, word-wrapped to max 2 lines |
| `config` | Yes | — | S3 bucket and path configuration |
| `config.source_bucket` | Yes | — | Bucket containing background images |
| `config.dest_bucket` | Yes | — | Bucket for output images |
| `config.public_path` | Yes | — | Path prefix for output images |
| `background_source` | Yes | — | Background image name (without `.jpg`) |
| `output_format` | No | `jpg` | `jpg` or `png` |
| `output_quality` | No | `85` | JPEG quality (1-100), ignored for PNG |
| `is_thumbnail` | No | `false` | Also generate a thumbnail variant |

## S3 Paths

| Direction | Path Pattern |
|-----------|-------------|
| Trigger input | `actions/{job_id}.json` |
| Background image | `backgrounds/{background_source}.jpg` |
| Output image | `{config.public_path}/{product_part_number}.{format}` |
| Thumbnail | `{config.public_path}/{product_part_number}_thumb.{format}` |

## Text Overlay Layout

| Element | Font Size | Max Lines | Wrap Width | Position |
|---------|-----------|-----------|------------|----------|
| Title | 40px | 3 | 30 chars | Y=80, X=60 margin |
| Authors | 24px | 2 | 40 chars | Below title + 40px gap |

- Long titles are word-wrapped and truncated with `...` if they exceed max lines.
- Text is rendered in white on the background image.
- Font loading tries system paths (DejaVu, Liberation, Helvetica) then falls back to Pillow's default bitmap font.

## Invocation

**S3 event trigger (primary):**

Triggers on `s3:ObjectCreated:*` for the `actions/*.json` prefix.

**Direct invocation (testing):**
```json
{
  "bucket": "trigger-bucket",
  "key": "actions/job-001.json"
}
```

**Response:**
```json
{
  "statusCode": 200,
  "body": {
    "output_key": "images/products/STD-12345.jpg",
    "thumbnail_key": "",
    "width": 800,
    "height": 600,
    "format": "jpg"
  }
}
```

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Invalid trigger JSON schema | Returns 400, trigger JSON preserved |
| Background image not found | Returns 500 (S3 error), trigger JSON preserved |
| Upload failure | Returns 500, trigger JSON preserved |
| Unsupported output format | Falls back to `jpg` |
| Unexpected exception | Returns 500, trigger JSON preserved |
| Successful processing | Trigger JSON deleted |

The trigger JSON is only deleted after successful processing. On any failure, it remains in S3 for debugging and retry.

## Dependencies

- **Pillow** — Image manipulation and text rendering
- **boto3** — S3 read/write operations

## Tests

```bash
# Generator tests
python -m pytest tests/generators/test_image_overlay_generator.py -v

# Handler tests
python -m pytest tests/handlers/test_image_overlay_handler.py -v
```

28 generator tests + 12 handler tests covering: overlay rendering, text wrapping/truncation, thumbnail generation, output formats (JPEG/PNG), trigger validation, S3 errors, image encoding, event parsing, and error handling.
