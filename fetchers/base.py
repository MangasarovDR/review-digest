from dataclasses import dataclass


@dataclass
class Review:
    platform: str
    external_id: str
    author: str
    rating: float
    text: str
    review_date: str  # ISO 8601
