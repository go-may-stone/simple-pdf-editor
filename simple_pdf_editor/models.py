from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from pathlib import Path
import json
import uuid


TEXT_KIND = "text"
RECT_KIND = "rect"
ELLIPSE_KIND = "ellipse"
LINE_KIND = "line"
ARROW_KIND = "arrow"

SHAPE_KINDS = {RECT_KIND, ELLIPSE_KIND, LINE_KIND, ARROW_KIND}
ANNOTATION_KINDS = {TEXT_KIND, *SHAPE_KINDS}


@dataclass
class Annotation:
    id: str
    page: int
    kind: str
    x: float
    y: float
    w: float
    h: float
    z: int = 0
    text: str = ""
    font_family: str = "Meiryo"
    font_size: float = 14.0
    text_color: str = "#202124"
    alignment: str = "left"
    background_color: str = ""
    border_enabled: bool = False
    stroke_color: str = "#d93025"
    stroke_width: float = 2.0
    fill_color: str = "#fff176"
    fill_opacity: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Annotation":
        valid_names = {field.name for field in fields(cls)}
        kwargs = {name: data[name] for name in valid_names if name in data}
        if not kwargs.get("id"):
            kwargs["id"] = new_annotation_id()
        if kwargs.get("kind") not in ANNOTATION_KINDS:
            raise ValueError(f"Unsupported annotation kind: {kwargs.get('kind')}")
        return cls(**kwargs)


def new_annotation_id() -> str:
    return uuid.uuid4().hex


def annotations_to_dicts(annotations: list[Annotation]) -> list[dict]:
    return [annotation.to_dict() for annotation in annotations]


def annotations_from_dicts(items: list[dict]) -> list[Annotation]:
    return [Annotation.from_dict(item) for item in items]


def save_edit_file(path: str | Path, source_pdf: str | Path, annotations: list[Annotation]) -> None:
    edit_path = Path(path)
    payload = {
        "version": 1,
        "source_pdf": str(Path(source_pdf).resolve()),
        "annotations": annotations_to_dicts(annotations),
    }
    edit_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_edit_file(path: str | Path) -> tuple[str, list[Annotation]]:
    edit_path = Path(path)
    payload = json.loads(edit_path.read_text(encoding="utf-8"))
    source_pdf = payload.get("source_pdf")
    if not source_pdf:
        raise ValueError("Edit file does not contain source_pdf.")

    source_path = Path(source_pdf)
    if not source_path.is_absolute():
        source_path = (edit_path.parent / source_path).resolve()

    annotations = annotations_from_dicts(payload.get("annotations", []))
    return str(source_path), annotations
