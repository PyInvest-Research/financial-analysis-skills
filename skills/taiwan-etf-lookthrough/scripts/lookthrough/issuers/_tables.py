"""Read bounded static HTML and validate selected source table nodes."""

from ._assets import MAX_RAW_BYTES
from .fubon import _Document, _Node as Node, _decode


MAX_DEPTH = 128
MAX_NODES = 100_000


class _BoundedDocument(_Document):
    """Reuse the existing non-executing HTML tree with resource bounds."""

    def __init__(self, text: str) -> None:
        """Initialize a per-document node budget before parsing."""
        self.node_count = 0
        super().__init__(text)

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]],
    ) -> None:
        """Reject excessive depth or node counts before allocating nodes."""
        self.node_count += 1
        if len(self.stack) >= MAX_DEPTH or self.node_count > MAX_NODES:
            raise ValueError("HTML tree exceeds structural read cap")
        super().handle_starttag(tag, attrs)


def read_html(raw: bytes) -> Node:
    """Strictly decode at most 10 MiB without scripts, network or repairs."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_RAW_BYTES:
        raise ValueError("HTML source is empty or exceeds the read cap")
    return _BoundedDocument(_decode(raw)).root


def one(nodes: list[Node], label: str) -> Node:
    """Require exactly one complete source element for a required field."""
    if len(nodes) != 1 or not nodes[0].closed:
        raise ValueError(f"Missing, malformed or ambiguous {label}")
    return nodes[0]


def by_id(root: Node, element_id: str) -> Node:
    """Require one complete descendant with the specified source ID."""
    found: list[Node] = []
    pending = list(reversed(root.children))
    while pending:
        node = pending.pop()
        if node.attrs.get("id") == element_id:
            found.append(node)
        pending.extend(reversed(node.children))
    return one(found, element_id)


def has_class(node: Node, class_name: str) -> bool:
    """Match an exact HTML class token, retaining case sensitivity."""
    return class_name in (node.attrs.get("class") or "").split()


def rows(table: Node) -> list[list[str]]:
    """Read source-order th/td rows, rejecting nesting or missing closures."""
    if table.tag != "table" or not table.closed or table.find("table"):
        raise ValueError("Malformed or nested source table")
    result: list[list[str]] = []
    for row in table.find("tr"):
        cells = [cell for cell in row.children if cell.tag in ("th", "td")]
        if (not row.closed or not cells
                or any(not cell.closed for cell in cells)):
            raise ValueError("Malformed source table row")
        result.append([cell.text for cell in cells])
    if not result:
        raise ValueError("Empty source table")
    return result
