from dataclasses import asdict, dataclass, field
import hashlib


@dataclass
class Acquisition:
    format: str
    url: str
    media_type: str
    length: int | None = None


@dataclass
class Book:
    title: str
    authors: list[str]
    identifier: str
    source: str
    library_id: str = ""
    book_id: str | None = None
    updated: str = ""
    published: str = ""
    publication_date: str = ""
    description: str = ""
    cover_url: str | None = None
    acquisitions: list[Acquisition] = field(default_factory=list)

    @property
    def key(self):
        identity = self.identifier or self.book_id or '\n'.join(a.url for a in self.acquisitions)
        return hashlib.sha256(f'{self.source}\n{self.library_id}\n{identity}'.encode()).hexdigest()[:24]

    def to_dict(self):
        return {**asdict(self), 'key': self.key}

    @classmethod
    def from_dict(cls, data):
        data = dict(data)
        data.pop('key', None)
        data['acquisitions'] = [Acquisition(**a) for a in data['acquisitions']]
        return cls(**data)
