__all__ = ["PDFExtractor", "VideoTranscriber"]


def __getattr__(name):
    """Lazy imports to avoid pulling in heavy dependencies across Lambdas."""
    if name == "PDFExtractor":
        from .pdf_extractor import PDFExtractor
        return PDFExtractor
    if name == "VideoTranscriber":
        from .video_transcriber import VideoTranscriber
        return VideoTranscriber
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
