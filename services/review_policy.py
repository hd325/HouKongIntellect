"""Shared, side-effect-free publication policy for stored files and indexes."""
from collections.abc import Mapping


def is_published_record(record: Mapping[str, object]) -> bool:
    """Legacy records remain published; explicit review states fail closed.

    Personal uploads and teacher uploads receive ``published`` from the upload
    service. This policy does not infer permissions from the account or audience.
    """
    return "review_status" not in record or record.get("review_status") == "published"
