from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, Optional


def parse_tvcf_date(value: str) -> Optional[date]:
    if not value:
        return None

    value = value.strip()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        pass

    if len(value) >= 10:
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").date()
        except ValueError:
            return None

    return None


@dataclass
class MediaItem:
    idx: str = ""
    nidx: str = ""
    mcode: str = ""
    title: str = ""
    chapter: str = ""
    brand: str = ""
    published_date: str = ""
    registered_date: str = ""
    country_code: str = ""
    category_code: str = ""
    category_name: str = ""
    duration: Optional[float] = None
    play_url: str = ""
    source_page: str = ""
    stream_urls: Dict[str, str] = field(default_factory=dict)

    @property
    def display_title(self) -> str:
        parts = [self.title, self.chapter]
        return " ".join(part for part in parts if part).strip() or self.nidx or self.idx

    def date_value(self, basis: str) -> Optional[date]:
        if basis == "registered":
            return parse_tvcf_date(self.registered_date)
        return parse_tvcf_date(self.published_date)

    def date_label(self, basis: str = "published") -> str:
        value = self.date_value(basis)
        return value.strftime("%Y-%m-%d") if value else "0000-00-00"
