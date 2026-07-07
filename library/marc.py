"""Self-contained MARC21 reader/writer (ISO 2709 binary + MARCXML).

No external dependency: libraries exchange bibliographic records as MARC, so we
parse both the binary ``.mrc`` (ISO 2709) and MARCXML forms into a small
intermediate representation, map records to the existing catalog-import row
shape (so MARC flows through the staged import pipeline), and serialize our
catalog back out to MARC for export.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET  # used only to *build*/serialize XML

# Parse untrusted uploads with the hardened parser (blocks XXE / entity-expansion
# "billion laughs" attacks that the stdlib parser is vulnerable to).
from defusedxml.ElementTree import fromstring as _safe_fromstring

FIELD_TERMINATOR = b"\x1e"
RECORD_TERMINATOR = b"\x1d"
SUBFIELD_DELIMITER = b"\x1f"
MARCXML_NS = "http://www.loc.gov/MARC21/slim"


@dataclass
class MarcField:
    tag: str
    indicators: tuple[str, str] = (" ", " ")
    subfields: list[tuple[str, str]] = field(default_factory=list)
    data: str = ""  # control fields (tag < "010")

    @property
    def is_control(self) -> bool:
        return self.tag < "010"

    def first(self, code: str) -> str:
        for sub_code, value in self.subfields:
            if sub_code == code:
                return value
        return ""

    def all(self, code: str) -> list[str]:
        return [value for sub_code, value in self.subfields if sub_code == code]


@dataclass
class MarcRecord:
    leader: str = "00000nam a2200000 a 4500"
    fields: list[MarcField] = field(default_factory=list)

    def get(self, tag: str) -> list[MarcField]:
        return [f for f in self.fields if f.tag == tag]

    def first(self, tag: str, code: str) -> str:
        for f in self.get(tag):
            value = f.first(code)
            if value:
                return value
        return ""


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def parse_marc(content: bytes | str) -> list[MarcRecord]:
    """Parse MARCXML or binary ISO 2709 into records (auto-detected)."""
    if isinstance(content, str):
        raw = content.encode("utf-8")
    else:
        raw = content
    stripped = raw.lstrip()
    if stripped[:1] == b"<":
        return parse_marcxml(raw)
    return parse_iso2709(raw)


def parse_iso2709(data: bytes) -> list[MarcRecord]:
    records: list[MarcRecord] = []
    pos = 0
    length_of = len(data)
    while pos < length_of:
        # Skip stray separators/whitespace between records.
        if data[pos:pos + 1] in (b"\n", b"\r", b" ", RECORD_TERMINATOR):
            pos += 1
            continue
        try:
            record_length = int(data[pos:pos + 5])
        except ValueError:
            break
        chunk = data[pos:pos + record_length]
        pos += record_length
        record = _parse_iso2709_record(chunk)
        if record is not None:
            records.append(record)
    return records


def _parse_iso2709_record(chunk: bytes) -> MarcRecord | None:
    if len(chunk) < 24:
        return None
    leader = chunk[:24].decode("utf-8", "replace")
    try:
        base_address = int(leader[12:17])
    except ValueError:
        return None
    directory = chunk[24:base_address - 1]  # minus the field terminator
    entries = [directory[i:i + 12] for i in range(0, len(directory), 12)]
    record = MarcRecord(leader=leader)
    for entry in entries:
        if len(entry) < 12:
            continue
        tag = entry[0:3].decode("ascii", "replace")
        flen = int(entry[3:7])
        start = int(entry[7:12])
        raw_field = chunk[base_address + start: base_address + start + flen]
        raw_field = raw_field.rstrip(FIELD_TERMINATOR)
        if tag < "010":
            record.fields.append(MarcField(tag=tag, data=raw_field.decode("utf-8", "replace")))
            continue
        text = raw_field.decode("utf-8", "replace")
        ind1, ind2 = (text[0:1] or " "), (text[1:2] or " ")
        subfields: list[tuple[str, str]] = []
        for part in raw_field[2:].split(SUBFIELD_DELIMITER):
            if not part:
                continue
            decoded = part.decode("utf-8", "replace")
            subfields.append((decoded[0], decoded[1:]))
        record.fields.append(MarcField(tag=tag, indicators=(ind1, ind2), subfields=subfields))
    return record


def parse_marcxml(data: bytes) -> list[MarcRecord]:
    root = _safe_fromstring(data)
    records: list[MarcRecord] = []
    record_elems = root.iter(f"{{{MARCXML_NS}}}record")
    # Also support namespace-less MARCXML.
    record_elems = list(record_elems) or list(root.iter("record"))
    if root.tag.endswith("record"):
        record_elems = [root]
    for rec_el in record_elems:
        record = MarcRecord(fields=[])
        for child in rec_el:
            tag_name = child.tag.split("}")[-1]
            if tag_name == "leader":
                record.leader = child.text or record.leader
            elif tag_name == "controlfield":
                record.fields.append(MarcField(tag=child.get("tag", ""), data=child.text or ""))
            elif tag_name == "datafield":
                subs = [
                    (s.get("code", ""), s.text or "")
                    for s in child
                    if s.tag.split("}")[-1] == "subfield"
                ]
                record.fields.append(
                    MarcField(
                        tag=child.get("tag", ""),
                        indicators=(child.get("ind1", " "), child.get("ind2", " ")),
                        subfields=subs,
                    )
                )
        records.append(record)
    return records


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def to_iso2709(records: list[MarcRecord]) -> bytes:
    return b"".join(_record_to_iso2709(r) for r in records)


def _record_to_iso2709(record: MarcRecord) -> bytes:
    directory = io.BytesIO()
    field_data = io.BytesIO()
    for f in record.fields:
        if f.is_control:
            encoded = f.data.encode("utf-8") + FIELD_TERMINATOR
        else:
            body = f"{f.indicators[0]}{f.indicators[1]}".encode()
            for code, value in f.subfields:
                body += SUBFIELD_DELIMITER + code.encode("utf-8") + value.encode("utf-8")
            encoded = body + FIELD_TERMINATOR
        start = field_data.tell()
        directory.write(f"{f.tag:>3}{len(encoded):04d}{start:05d}".encode("ascii"))
        field_data.write(encoded)
    directory_bytes = directory.getvalue() + FIELD_TERMINATOR
    body_bytes = field_data.getvalue() + RECORD_TERMINATOR
    base_address = 24 + len(directory_bytes)
    record_length = base_address + len(body_bytes)
    leader = f"{record_length:05d}nam a22{base_address:05d} a 4500"
    return leader.encode("ascii") + directory_bytes + body_bytes


def to_marcxml(records: list[MarcRecord]) -> bytes:
    ET.register_namespace("", MARCXML_NS)
    collection = ET.Element(f"{{{MARCXML_NS}}}collection")
    for record in records:
        rec_el = ET.SubElement(collection, f"{{{MARCXML_NS}}}record")
        leader_el = ET.SubElement(rec_el, f"{{{MARCXML_NS}}}leader")
        leader_el.text = record.leader
        for f in record.fields:
            if f.is_control:
                cf = ET.SubElement(rec_el, f"{{{MARCXML_NS}}}controlfield", {"tag": f.tag})
                cf.text = f.data
            else:
                df = ET.SubElement(
                    rec_el,
                    f"{{{MARCXML_NS}}}datafield",
                    {"tag": f.tag, "ind1": f.indicators[0], "ind2": f.indicators[1]},
                )
                for code, value in f.subfields:
                    sf = ET.SubElement(df, f"{{{MARCXML_NS}}}subfield", {"code": code})
                    sf.text = value
    return ET.tostring(collection, encoding="utf-8", xml_declaration=True)


# --------------------------------------------------------------------------- #
# Mapping to/from the catalog
# --------------------------------------------------------------------------- #
def _clean(value: str) -> str:
    return (value or "").strip().rstrip("/,:;").strip()


def marc_record_to_import_row(record: MarcRecord) -> dict:
    """Map a MARC bibliographic record to the catalog-import row dict."""
    title = _clean(record.first("245", "a"))
    subtitle = _clean(record.first("245", "b"))
    authors = []
    for tag in ("100", "110", "700", "710"):
        for f in record.get(tag):
            name = _clean(f.first("a"))
            if name:
                authors.append(name)
    subjects = []
    for tag in ("650", "651", "600"):
        for f in record.get(tag):
            subj = _clean(f.first("a"))
            if subj:
                subjects.append(subj)
    isbn = ""
    for f in record.get("020"):
        candidate = f.first("a").replace("-", "").split()[0] if f.first("a") else ""
        if len(candidate) == 13 and candidate.isdigit():
            isbn = candidate
            break
    publisher = _clean(record.first("264", "b") or record.first("260", "b"))
    date_raw = _clean(record.first("264", "c") or record.first("260", "c"))
    year = "".join(ch for ch in date_raw if ch.isdigit())[:4]
    edition = _clean(record.first("250", "a"))
    summary = _clean(record.first("520", "a"))
    row = {
        "title": title,
        "subtitle": subtitle,
        "authors": ";".join(authors),
        "subjects": ";".join(subjects),
        "isbn_13": isbn,
        "publisher": publisher,
        "publication_year": year,
        "edition_statement": edition,
        "summary": summary,
    }
    return {k: v for k, v in row.items() if v}


def marc_rows_from_content(content: bytes | str) -> list[dict]:
    return [marc_record_to_import_row(r) for r in parse_marc(content)]


def edition_to_marc_record(edition) -> MarcRecord:
    """Build a MARC record from an Edition (+ its Work) for export."""
    work = edition.work
    record = MarcRecord(fields=[])
    if edition.isbn_13:
        record.fields.append(MarcField("020", subfields=[("a", edition.isbn_13)]))
    authors = list(work.authors.all())
    if authors:
        record.fields.append(
            MarcField("100", indicators=("1", " "), subfields=[("a", authors[0].name)])
        )
    title_subs = [("a", work.canonical_title)]
    if work.subtitle:
        title_subs.append(("b", work.subtitle))
    record.fields.append(MarcField("245", indicators=("1", "0"), subfields=title_subs))
    if edition.edition_statement:
        record.fields.append(MarcField("250", subfields=[("a", edition.edition_statement)]))
    pub_subs = []
    if edition.publisher:
        pub_subs.append(("b", edition.publisher))
    if edition.publication_year:
        pub_subs.append(("c", str(edition.publication_year)))
    if pub_subs:
        record.fields.append(MarcField("264", indicators=(" ", "1"), subfields=pub_subs))
    if work.summary:
        record.fields.append(MarcField("520", subfields=[("a", work.summary[:900])]))
    for subject in work.subjects.all():
        record.fields.append(MarcField("650", indicators=(" ", "0"), subfields=[("a", subject.name)]))
    for author in authors[1:]:
        record.fields.append(
            MarcField("700", indicators=("1", " "), subfields=[("a", author.name)])
        )
    return record
